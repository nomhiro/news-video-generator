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

/**
 * 挿絵を帯に対して拡大する比率。ドリフトの可動域はこの拡大ぶんから取る。
 *
 * **`objectFit: cover` のまま拡大する方式を維持する。** 一度 `contain` +
 * 縮小配置（`IMAGE_SCALE = 0.95`）に変えたが、**画像の地と地（`Background`）の
 * 色が一致しないため四角い継ぎ目が見えた**（実測: 画像の地 rgb(24,23,25) に
 * 対して地は rgb(30,29,33)。テーマの `bg` は #1b1a1d = rgb(27,26,29) だが、
 * `Background` の陰影で持ち上がる。画像側も指示した #1b1a1d ちょうどには
 * ならない）。切り取りの危険は条件付き（モデルが余白の指示に従わなければ）
 * だが、継ぎ目は**常に見える**。常時の欠点のほうが重い。
 *
 * 代わりに、生成側へ「図と名札を中央90%の内側に置く」ことを要求する
 * （`ILLUSTRATION_STYLE_PROMPT` の Margins）。下の可動域はその 90% と
 * 両立する値に抑えてある。
 */
const DRIFT_SCALE = 1.06;

/**
 * 水平ドリフトの振れ幅（%）。**手で決めずに `DRIFT_SCALE` から導く。**
 *
 * 以前は上限を `(DRIFT_SCALE - 1) / 2 * 100` と書いていた。**この式は誤り。**
 * CSS の `transform` は右から左に適用されるため、`translate` の%は
 * **scale で拡大された後に効く**。正しい上限は scale で割った値である。
 * 手で計算した 3% は正しい上限 2.83% を超えており、両端で画像の外側が
 * 見えるところまで動いていた——実測では末尾フレームで右端 x=1079 まで
 * 描画が達し、名札「軽ブロック」が切れていた（2026-08-20）。
 *
 * 二度と手計算しないよう、可動域そのものをここで導出する。安全率 0.6 は
 * 「中央90%の内側」という生成側への要求と両立させるためのもの——この値だと
 * 可視範囲は最悪でも横幅の 0.4%〜95.4% になり、95% の内側に置かれた図は
 * 切れない。上げると 90% の要求では足りなくなる。
 */
const DRIFT_X_PCT = ((DRIFT_SCALE - 1) / 2 / DRIFT_SCALE) * 100 * 0.6;

/** 垂直ドリフトの振れ幅（%）。横より控えめにして「奥へ流れる」印象を作る。 */
const DRIFT_Y_PCT = DRIFT_X_PCT * 0.6;

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
          //
          // **フェードの開始を 78% から 88% に遅らせた。** 名札が図の下部に
          // 来ることがあり、22%も透かすと文字が読めなくなる。帯の下辺を
          // 地へ繋ぐ役割は残すので、フェード自体は無くさない。
          WebkitMaskImage: "linear-gradient(to bottom, black 0%, black 88%, transparent 100%)",
          maskImage: "linear-gradient(to bottom, black 0%, black 88%, transparent 100%)",
        }}
      />
    </div>
  );
};
