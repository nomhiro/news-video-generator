import { AbsoluteFill, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { COLORS, FONT_STACK } from "../theme";
import { Headline } from "./Headline";

/** 対比する2つを左右に並べる。要素が順に現れる。 */
export const Compare: React.FC<{ headline: string; items: string[] }> = ({
  headline,
  items,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  return (
    <AbsoluteFill
      style={{ justifyContent: "center", alignItems: "center", padding: 64 }}
    >
      <Headline text={headline} />
      <div style={{ display: "flex", gap: 40, marginTop: 88 }}>
        {items.map((item, i) => {
          // delay をずらすのが「順に出る」演出の要。
          const enter = spring({
            frame: frame - i * 8,
            fps,
            config: { damping: 200 },
          });
          return (
            <div
              key={item}
              style={{
                width: 380,
                height: 380,
                borderRadius: 32,
                backgroundColor: "rgba(255,255,255,0.06)",
                border: `4px solid ${i === 0 ? COLORS.accent : COLORS.accent2}`,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                transform: `translateY(${(1 - enter) * 80}px) scale(${0.9 + enter * 0.1})`,
                opacity: enter,
              }}
            >
              <span
                style={{
                  fontFamily: FONT_STACK,
                  fontSize: 84,
                  fontWeight: 800,
                  color: COLORS.text,
                }}
              >
                {item}
              </span>
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
