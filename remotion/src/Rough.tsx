import { useMemo } from "react";
import rough from "roughjs";
import type { Drawable } from "roughjs/bin/core";

/**
 * roughjs による手描き風プリミティブ。
 *
 * **すべての図形は呼び出し側が固定 seed を渡す。** rough.js は既定で
 * `Math.random()` を使って線を揺らす。Remotion はコンポーネントを
 * フレームごとに再評価するため、seed を固定しないと1フレームごとに
 * 線形が変わり、書き出した動画では激しいちらつきになる（実測で確認済み）。
 * seed 以外の引数（サイズ・色）が変わらない限り、同じ線を返す。
 */
const generator = rough.generator();

type PathKind = "outline" | "fill";
type PathSet = { d: string; kind: PathKind };

/**
 * Drawable の sets を outline（枠線）と fill（ハッチング塗り）に分ける。
 *
 * roughjs の `PathInfo`（`generator.toPaths`）はこの区別を返さないため、
 * `set.type` を直接見る。fillStyle: "hachure" の塗りは実際には
 * 斜線のストローク（type: "fillSketch"）として描かれる。
 */
function toPathSets(drawable: Drawable): PathSet[] {
  return drawable.sets.map((set) => ({
    d: generator.opsToPath(set),
    kind: set.type === "path" ? "outline" : "fill",
  }));
}

/** アウトラインを `drawProgress` に応じて描き切る `<path>` 群。 */
const OutlinePaths: React.FC<{
  sets: PathSet[];
  stroke: string;
  strokeWidth: number;
  progress: number;
}> = ({ sets, stroke, strokeWidth, progress }) => (
  <>
    {sets
      .filter((s) => s.kind === "outline")
      .map((s, i) => (
        <path
          key={`o${i}`}
          d={s.d}
          fill="none"
          stroke={stroke}
          strokeWidth={strokeWidth}
          strokeLinecap="round"
          // pathLength=1 で d の実長にかかわらず正規化する。実際のベクタパスの
          // 長さを事前に測る必要がなくなる（roughjs の出す曲線近似の弧長を
          // 計算せずに「自分の線を描いている」動きを作れる）。
          pathLength={1}
          strokeDasharray="1 1"
          strokeDashoffset={1 - progress}
        />
      ))}
  </>
);

/** ハッチング塗りを `fillOpacity` でフェードインさせる `<path>` 群。 */
const FillPaths: React.FC<{ sets: PathSet[]; color: string; opacity: number }> = ({
  sets,
  color,
  opacity,
}) => (
  <>
    {sets
      .filter((s) => s.kind === "fill")
      .map((s, i) => (
        <path key={`f${i}`} d={s.d} fill="none" stroke={color} strokeWidth={1.6} opacity={opacity} />
      ))}
  </>
);

/**
 * 手描き風の矩形。箱の枠線を描き切ってから、ハッチング塗りをフェードインさせる
 * （スケッチはまず輪郭を描き、そのあとに影/塗りを入れる）。
 */
export const RoughRect: React.FC<{
  width: number;
  height: number;
  seed: number;
  stroke: string;
  fill: string;
  drawProgress: number;
  fillOpacity: number;
}> = ({ width, height, seed, stroke, fill, drawProgress, fillOpacity }) => {
  const drawable = useMemo(
    () =>
      generator.rectangle(3, 3, Math.max(width - 6, 1), Math.max(height - 6, 1), {
        seed,
        roughness: 1.6,
        strokeWidth: 3.5,
        stroke,
        fill,
        fillStyle: "hachure",
        hachureGap: 9,
      }),
    [width, height, seed, stroke, fill],
  );
  const sets = useMemo(() => toPathSets(drawable), [drawable]);

  return (
    <svg
      width={width}
      height={height}
      style={{ position: "absolute", inset: 0, overflow: "visible" }}
    >
      <OutlinePaths sets={sets} stroke={stroke} strokeWidth={3.5} progress={drawProgress} />
      <FillPaths sets={sets} color={fill} opacity={fillOpacity} />
    </svg>
  );
};

/**
 * 2要素をつなぐ手描き風のコネクタ。
 *
 * `flow` は縦の矢印（原因→結果、方向がある）、`compare` は横の線
 * （対比、方向はない）に使う。矢印の描画は「線を引く → 矢先を引く」の
 * 2段に分け、`drawProgress` の 0→0.7 を線、0.7→1 を矢先に割り当てる
 * （手が線を引いてから矢先を付け足す動きに近い）。
 */
export const RoughConnector: React.FC<{
  width: number;
  height: number;
  seed: number;
  stroke: string;
  orientation: "vertical" | "horizontal";
  arrow: boolean;
  drawProgress: number;
}> = ({ width, height, seed, stroke, orientation, arrow, drawProgress }) => {
  const options = { seed, roughness: 1.3, strokeWidth: 4, stroke };

  const line = useMemo(() => {
    if (orientation === "vertical") {
      const end = height - (arrow ? 22 : 4);
      return generator.line(width / 2, 4, width / 2, Math.max(end, 4), options);
    }
    return generator.line(4, height / 2, Math.max(width - 4, 4), height / 2, options);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [width, height, seed, stroke, orientation, arrow]);

  const head = useMemo(() => {
    if (!arrow || orientation !== "vertical") return null;
    const cx = width / 2;
    const tip = height - 4;
    return generator.linearPath(
      [
        [cx - 16, tip - 26],
        [cx, tip],
        [cx + 16, tip - 26],
      ],
      { ...options, seed: seed + 1 },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [width, height, seed, stroke, orientation, arrow]);

  // 線を 0→0.7、矢先を 0.7→1 に割り当てる（矢印が無いときは線が 0→1 全体を使う）。
  const lineEnd = arrow ? 0.7 : 1;
  const lineProgress = Math.max(0, Math.min(1, drawProgress / lineEnd));
  const headProgress = arrow
    ? Math.max(0, Math.min(1, (drawProgress - lineEnd) / (1 - lineEnd)))
    : 0;

  const lineSets = useMemo(() => toPathSets(line).filter((s) => s.kind === "outline"), [line]);
  const headSets = useMemo(
    () => (head ? toPathSets(head).filter((s) => s.kind === "outline") : []),
    [head],
  );

  return (
    <svg width={width} height={height} style={{ position: "absolute", inset: 0, overflow: "visible" }}>
      <OutlinePaths sets={lineSets} stroke={stroke} strokeWidth={4} progress={lineProgress} />
      {head && <OutlinePaths sets={headSets} stroke={stroke} strokeWidth={4} progress={headProgress} />}
    </svg>
  );
};
