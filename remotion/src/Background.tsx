import { AbsoluteFill, useCurrentFrame, useVideoConfig } from "remotion";
import { COLORS } from "./theme";

/**
 * 背景。角度が動くグラデーションだけで作る。
 *
 * **filter: blur() を使わない。** 全画面 1080x1920 への blur(40px) を
 * 2枚重ねた版で実測したところ、2 vCPU でのレンダリングが
 * 199秒 → 598秒（3倍）になった。グローを出したいときも blur ではなく
 * グラデーションと不透明度で作る。
 */
export const Background: React.FC = () => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const t = durationInFrames > 0 ? frame / durationInFrames : 0;
  return (
    <AbsoluteFill
      style={{
        background: `linear-gradient(${120 + t * 60}deg, ${COLORS.bg} 0%, #16204a 55%, #241436 100%)`,
      }}
    />
  );
};
