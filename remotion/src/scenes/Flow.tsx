import { AbsoluteFill, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { COLORS, FONT_STACK } from "../theme";
import { Headline } from "./Headline";

/** 原因 → 結果を矢印で繋ぐ。上から下に流す（縦画面なので縦に並べる）。 */
export const Flow: React.FC<{ headline: string; items: string[] }> = ({
  headline,
  items,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const arrow = spring({ frame: frame - 10, fps, config: { damping: 200 } });
  return (
    <AbsoluteFill
      style={{ justifyContent: "center", alignItems: "center", padding: 64 }}
    >
      <Headline text={headline} />
      <div
        style={{
          marginTop: 80,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 24,
        }}
      >
        <Box text={items[0]} color={COLORS.accent} frame={frame} fps={fps} delay={0} />
        <span
          style={{
            fontSize: 88,
            color: COLORS.subtle,
            opacity: arrow,
            transform: `translateY(${(1 - arrow) * -20}px)`,
          }}
        >
          ↓
        </span>
        <Box text={items[1]} color={COLORS.accent2} frame={frame} fps={fps} delay={18} />
      </div>
    </AbsoluteFill>
  );
};

const Box: React.FC<{
  text: string;
  color: string;
  frame: number;
  fps: number;
  delay: number;
}> = ({ text, color, frame, fps, delay }) => {
  const enter = spring({ frame: frame - delay, fps, config: { damping: 200 } });
  return (
    <div
      style={{
        width: 640,
        height: 220,
        borderRadius: 28,
        backgroundColor: "rgba(255,255,255,0.06)",
        border: `4px solid ${color}`,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        transform: `translateY(${(1 - enter) * 60}px)`,
        opacity: enter,
      }}
    >
      <span
        style={{
          fontFamily: FONT_STACK,
          fontSize: 76,
          fontWeight: 800,
          color: COLORS.text,
        }}
      >
        {text}
      </span>
    </div>
  );
};
