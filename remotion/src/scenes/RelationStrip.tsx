import type { Reveal } from "../beats";
import { fitFontSize } from "../fitText";
import { COLORS, FONT_STACK } from "../theme";
import type { Zone } from "../zones";

/** 項目ラベルの上限サイズ。1行の帯なので `DiagramBox` の名札より小さい。 */
const ITEM_MAX_SIZE = 44;
/** 関係ラベルの上限サイズ。 */
const RELATION_MAX_SIZE = 38;
/** コネクタ（線・矢印）の幅。関係ラベルはこの上に重ねて浮かせる。 */
const CONNECTOR_WIDTH = 140;
const CONNECTOR_HEIGHT = 32;
/** 項目とコネクタの間の隙間。 */
const GAP = 28;
/** ストリップの左右に残す余白。 */
const SIDE_PADDING = 56;
/** 関係ラベルのプレートの左右パディング。 */
const RELATION_PLATE_PADDING_X = 14;

/**
 * まっすぐな線（と、必要なら矢先）を左から右へ描き切るフラットなコネクタ。
 *
 * 以前は roughjs の手描き風コネクタ（`Rough.tsx` の `RoughConnector`）を
 * 使っていたが、restyle でスケッチ調をやめたのでここも直線に置き換える。
 * `drawProgress` に応じて `strokeDashoffset` で線を描き切る手法自体は
 * 引き継ぐ——`pathLength={1}` で正規化しているので、直線でも矢印でも
 * 同じ考え方で「線が伸びる」動きが作れる。矢印は 0→0.7 を線、0.7→1 を
 * 矢先に割り当てる（`RoughConnector` と同じ配分）。
 */
const FlatConnector: React.FC<{
  width: number;
  height: number;
  stroke: string;
  arrow: boolean;
  drawProgress: number;
}> = ({ width, height, stroke, arrow, drawProgress }) => {
  const y = height / 2;
  const headSize = 22;
  const lineEndX = arrow ? width - headSize : width - 4;
  const lineEnd = arrow ? 0.7 : 1;
  const lineProgress = Math.max(0, Math.min(1, drawProgress / lineEnd));
  const headProgress = arrow
    ? Math.max(0, Math.min(1, (drawProgress - lineEnd) / (1 - lineEnd)))
    : 0;

  return (
    <svg width={width} height={height} style={{ position: "absolute", inset: 0, overflow: "visible" }}>
      <line
        x1={4}
        y1={y}
        x2={Math.max(lineEndX, 4)}
        y2={y}
        stroke={stroke}
        strokeWidth={4}
        strokeLinecap="round"
        pathLength={1}
        strokeDasharray="1 1"
        strokeDashoffset={1 - lineProgress}
      />
      {arrow && (
        <polygon
          points={`${width - headSize},${y - 12} ${width - 4},${y} ${width - headSize},${y + 12}`}
          fill={stroke}
          opacity={headProgress}
        />
      )}
    </svg>
  );
};

/**
 * 対比（`compare`）/ 因果（`flow`）の2要素を、挿絵の下の1行だけの帯で示す。
 *
 * 以前は箱2つ＋コネクタを図の帯（720px）いっぱいに描いていたが、その帯は
 * 共有の挿絵に明け渡した。**概念（2要素とその関係）は消さず、高さだけ
 * 120pxに圧縮する**——`items` / `relation` はスキーマ上 LLM に必須で出させて
 * いるフィールドなので、描く場所を残さないとデータが腐る（誰も見ない値に
 * なる）。コネクタは `FlatConnector`（直線・矢印）を使う。
 *
 * `arrow` で `compare`（対称、方向なし＝線）と `flow`（因果、方向あり＝矢印）
 * を切り分ける。ここでは向き（水平）は固定——縦画面では1行の帯に縦の矢印は
 * 収まらない。
 */
export const RelationStrip: React.FC<{
  items: string[];
  relation: string;
  arrow: boolean;
  zone: Zone;
  frameWidth: number;
  boxA: Reveal;
  boxB: Reveal;
  relationReveal: Reveal;
}> = ({ items, relation, arrow, zone, frameWidth, boxA, boxB, relationReveal }) => {
  // 項目ラベルに使える幅。中央のコネクタとその両脇の隙間を先に引く
  // （`Compare.tsx` が箱の幅を決めていたときと同じ「中央列を先に確保する」順序）。
  const itemWidth = (frameWidth - SIDE_PADDING * 2 - CONNECTOR_WIDTH - GAP * 2) / 2;
  // **2項目のラベルは同じサイズにする。** 片方だけ大きいと対比/因果の
  // 対等な関係に見えない（`Compare.tsx` の `labelCap` と同じ理由）。
  const itemSize = Math.min(
    ...items.map((item) => fitFontSize(item, itemWidth, ITEM_MAX_SIZE)),
  );
  const relationSize = fitFontSize(
    relation,
    CONNECTOR_WIDTH - RELATION_PLATE_PADDING_X * 2,
    RELATION_MAX_SIZE,
  );

  return (
    <div
      style={{
        position: "absolute",
        top: zone.top,
        height: zone.height,
        left: 0,
        right: 0,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        gap: GAP,
      }}
    >
      {/* items はスキーマが常に2要素であることを保証しているので固定 index。 */}
      <span
        style={{
          fontFamily: FONT_STACK,
          fontSize: itemSize,
          fontWeight: 800,
          color: COLORS.accent,
          opacity: boxA.opacity,
          whiteSpace: "nowrap",
        }}
      >
        {items[0]}
      </span>
      <div
        style={{
          position: "relative",
          width: CONNECTOR_WIDTH,
          height: CONNECTOR_HEIGHT,
          opacity: relationReveal.opacity,
        }}
      >
        <FlatConnector
          width={CONNECTOR_WIDTH}
          height={CONNECTOR_HEIGHT}
          stroke={COLORS.subtle}
          arrow={arrow}
          drawProgress={relationReveal.drawProgress}
        />
        {/* 線の真上ではなく上に離して置く（線とラベルが重なる不具合は
            `Compare.tsx` の旧実装で実測済み）。帯の高さが120pxしか無いので、
            離す距離は控えめにする。 */}
        <span
          style={{
            position: "absolute",
            top: -relationSize * 1.05,
            left: "50%",
            transform: "translateX(-50%)",
            fontFamily: FONT_STACK,
            fontSize: relationSize,
            fontWeight: 900,
            color: COLORS.text,
            backgroundColor: COLORS.plate,
            padding: `2px ${RELATION_PLATE_PADDING_X}px`,
            borderRadius: 8,
            whiteSpace: "nowrap",
          }}
        >
          {relation}
        </span>
      </div>
      <span
        style={{
          fontFamily: FONT_STACK,
          fontSize: itemSize,
          fontWeight: 800,
          color: COLORS.accent2,
          opacity: boxB.opacity,
          whiteSpace: "nowrap",
        }}
      >
        {items[1]}
      </span>
    </div>
  );
};
