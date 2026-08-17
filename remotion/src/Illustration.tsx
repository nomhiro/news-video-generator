import { Img, interpolate, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { useZones } from "./zones";

/**
 * 動画全体で共有する挿絵1枚。フレームの上52%を占める。
 *
 * **`NewsVideo` のトップレベル（すべての `<Sequence>` の外）から呼ぶこと。**
 * `useCurrentFrame()` は `<Sequence>` の内側では「そのシーケンス基準」の
 * 相対フレームを返すため、シーンごとに描くとドリフトがシーン切替のたびに
 * リセットされる（6シーンなら6回、ちらつきの原因になる）。トップレベルなら
 * 絶対フレームが取れるので、動画全体を通してなめらかに動く——1枚の画像
 * 要素で済む（シーンごとに6個描くより軽い）ぶんもここに乗る。
 *
 * `filename` が空文字列のときは何も描かない。地（`Background`）だけの
 * フォールバックは Python 側が挿絵生成の失敗時に意図して選ぶ状態なので、
 * ここで代替の図形を描いて「壊れて見える」ようにしない。
 */

/** スケールで確保する余白（%）。この半分までがドリフトの安全な可動域。 */
const DRIFT_SCALE = 1.06;

/** 水平ドリフトの振れ幅（%）。`(DRIFT_SCALE - 1) / 2 * 100` を超えない値にする
 * ——超えると拡大した画像の外側（余白）が見えてしまう。 */
const DRIFT_X_PCT = 3;

/** 垂直ドリフトの振れ幅（%）。横より控えめにして「奥へ流れる」印象を作る。 */
const DRIFT_Y_PCT = 1.8;

export const Illustration: React.FC<{ filename: string }> = ({ filename }) => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  // 挿絵の帯はレイアウトに関わらず同じなので、どちらの overload でもよい。
  const zone = useZones("statement").illustration;

  if (!filename) return null;

  // 動画全体（0〜durationInFrames-1）を通して一方向にゆっくり流す。
  // シーンごとの reveal（`beats.ts`）とは無関係の、独立した時間軸。
  const t = durationInFrames > 1 ? frame / (durationInFrames - 1) : 0;
  const translateX = interpolate(t, [0, 1], [-DRIFT_X_PCT, DRIFT_X_PCT]);
  const translateY = interpolate(t, [0, 1], [DRIFT_Y_PCT, -DRIFT_Y_PCT]);

  return (
    <div
      style={{
        position: "absolute",
        top: zone.top,
        left: 0,
        width: "100%",
        height: zone.height,
        overflow: "hidden",
      }}
    >
      <Img
        src={staticFile(filename)}
        style={{
          position: "absolute",
          inset: 0,
          width: "100%",
          height: "100%",
          objectFit: "cover",
          transform: `scale(${DRIFT_SCALE}) translate(${translateX}%, ${translateY}%)`,
          // 下端を地へ溶け込ませる。`filter: blur()` は使えない
          // （2 vCPU で 199秒 → 598秒に悪化した実測が `remotion_renderer.py` に
          // ある）ので、代わりに mask-image のグラデーションで透明度を落とす。
          // ぼかしと違い描画コストはほぼ増えない。
          WebkitMaskImage: "linear-gradient(to bottom, black 0%, black 78%, transparent 100%)",
          maskImage: "linear-gradient(to bottom, black 0%, black 78%, transparent 100%)",
        }}
      />
    </div>
  );
};
