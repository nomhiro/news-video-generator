import { AbsoluteFill } from "remotion";
import { COLORS } from "./theme";

/**
 * 背景。ほぼフラットな地。
 *
 * 以前は角度が動くグラデーションだったが、動く地は「手描きスケッチ」の
 * 語法と噛み合わない（紙や黒板は動かない）。静的な radial-gradient で
 * わずかな深み（中心がやや明るい）だけ残し、フレームには依存させない。
 *
 * **filter: blur() を使わない。** 全画面 1080x1920 への blur(40px) を
 * 2枚重ねた版で実測したところ、2 vCPU でのレンダリングが
 * 199秒 → 598秒（3倍）になった。グローを出したいときも blur ではなく
 * グラデーションと不透明度で作る。
 */
export const Background: React.FC = () => (
  <AbsoluteFill
    style={{
      // 中心のハイライトは COLORS.bg より少し明るいだけの中間色。
      // **地を寒色のグラファイト（#14161a）に振ったので、ハイライトも
      // 同じ方向へ揃える**（以前は #232227 で暖色寄りだった）。片方だけ
      // 暖色に残すと、地の広い面にうっすら茶色い染みが出る。
      background: `radial-gradient(circle at 50% 38%, #1e2229 0%, ${COLORS.bg} 70%)`,
    }}
  />
);
