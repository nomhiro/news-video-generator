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
      background: `radial-gradient(circle at 50% 38%, #2c292f 0%, ${COLORS.bg} 70%)`,
    }}
  />
);
