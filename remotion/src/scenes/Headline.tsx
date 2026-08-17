import { spring, useCurrentFrame, useVideoConfig } from "remotion";
import { COLORS, FONT_STACK } from "../theme";

/** 見出し。`Script.text_overlays[i]` が入る。 */
export const Headline: React.FC<{ text: string; size?: number }> = ({
  text,
  size = 92,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const enter = spring({ frame, fps, config: { damping: 200 } });
  return (
    <h1
      style={{
        fontFamily: FONT_STACK,
        fontSize: size,
        fontWeight: 900,
        color: COLORS.text,
        textAlign: "center",
        lineHeight: 1.28,
        margin: 0,
        wordBreak: "auto-phrase",
        transform: `translateY(${(1 - enter) * 40}px)`,
        opacity: enter,
      }}
    >
      {text}
    </h1>
  );
};
