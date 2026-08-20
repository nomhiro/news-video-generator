import { Img, interpolate, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { COLORS } from "./theme";
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
 * 紙のカードの中で挿絵を縮めて置く比率。**1未満であることが本質。**
 *
 * ここは二度往復した。経緯を残す。
 *
 * 1. `objectFit: cover` + `scale(1.06)` で拡大——抽象モチーフなら端を切っても
 *    何も失わないので成立していた。
 * 2. 名札を許した時点で破綻。実測で末尾フレームの右端 x=1079 まで描画が達し、
 *    名札が切れた。そこで `contain` + 縮小に変えたが、当時は挿絵の地が暗く、
 *    画像の地 rgb(24,23,25) と地 rgb(30,29,33) の差で**四角い継ぎ目**が見えた。
 *    継ぎ目は常時見えるので、いったん cover に戻して生成側へ
 *    「図と名札を中央90%の内側に」と要求した。
 * 3. **モデルはその要求に従わなかった。** 2回連続で横幅のほぼ端まで描き、
 *    先頭フレームで左端 x=0、末尾フレームで右端 x=1079 に達した。
 *    指示に頼る方針そのものが誤りだった。
 * 4. **挿絵を白地の紙にしたことで `contain` が正解になった。** カードの地は
 *    意図的に地と違う色なので、帯全体を紙で塗れば継ぎ目という概念が消える。
 *    実測でも生成画像の紙は rgb(247,244,235)〜(249,246,238) で、
 *    指示した #f5f2ea = rgb(245,242,234) との差は2〜4段——明るい地の上では
 *    知覚できない。
 *
 * つまり「切り取らない」と「継ぎ目が出ない」を同時に満たせるのは、
 * **地を塗ったカードの中に contain で収める**形だけである。
 */
const IMAGE_SCALE = 0.94;

/**
 * 水平ドリフトの振れ幅（%）。**手で決めずに `IMAGE_SCALE` から導く。**
 *
 * 以前は上限を `(DRIFT_SCALE - 1) / 2 * 100` と手で計算していたが、
 * **この式は誤りだった。** CSS の `transform` は右から左に適用されるため、
 * `translate` の%は **scale が掛かった後に効く**。正しい上限は scale で
 * 割った値である。手で出した 3% は正しい上限 2.83% を超えており、両端で
 * 画像の外側が見えるところまで動いていた（実測で確認）。
 *
 * 二度と手計算しないよう、可動域そのものをここで導出する。安全率 0.8 は
 * 画像の縁がちょうどカードの縁に接する状態を避けるためのもの。
 */
const DRIFT_X_PCT = ((1 - IMAGE_SCALE) / 2 / IMAGE_SCALE) * 100 * 0.8;

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
        // **帯全体を紙で塗る。** `contain` で収めた挿絵の周囲に出る余白が
        // 画像の地と同じ色になり、カードの中に継ぎ目が出ない
        // （`IMAGE_SCALE` のコメント参照）。
        backgroundColor: COLORS.paper,
      }}
    >
      <Img
        src={staticFile(filename)}
        style={{
          position: "absolute",
          inset: 0,
          width: "100%",
          height: "100%",
          // **`cover` ではなく `contain`。** 名札を含む図は端まで描かれるので、
          // 切り取る方式では文字が欠ける（`IMAGE_SCALE` のコメント参照）。
          objectFit: "contain",
          transform: `scale(${IMAGE_SCALE}) translate(${translateX}%, ${translateY}%)`,
          // **下端のフェードは持たない。** 挿絵は白地の紙のカードなので、
          // 透明度を落とすと白が暗い地へ滲むだけで、溶け込むどころか汚れて
          // 見える。カードの縁は意図した境界として立てる。
          //
          // 以前（挿絵の地が暗かった頃）は mask-image のグラデーションで
          // 下端を落としていた。`filter: blur()` を使えない事情
          // （2 vCPU で 199秒 → 598秒の実測が `remotion_renderer.py` にある）は
          // 変わらないので、フェードを戻すなら再び mask-image を使うこと。
        }}
      />
    </div>
  );
};
