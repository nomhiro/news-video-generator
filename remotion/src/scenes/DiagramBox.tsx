import { useReveal } from "../beats";
import { RoughRect } from "../Rough";
import { COLORS, FONT_STACK } from "../theme";

/**
 * `compare` / `flow` で使う手描き風の箱。
 *
 * ラベルはハッチング塗りの上に直接置かない。斜線の塗りが `入力` /
 * `専門家選択` のような文字を貫くと読みにくくなる（実測で確認した不具合）。
 * 地の色に近いプレートを文字の背後に敷き、そこだけ塗りを避ける。
 */
export const DiagramBox: React.FC<{
  text: string;
  width: number;
  height: number;
  color: string;
  seed: number;
  reveal: ReturnType<typeof useReveal>;
  fontSize?: number;
}> = ({ text, width, height, color, seed, reveal, fontSize = 84 }) => (
  <div style={{ position: "relative", width, height, opacity: reveal.opacity }}>
    <RoughRect
      width={width}
      height={height}
      seed={seed}
      stroke={color}
      fill={color}
      drawProgress={reveal.drawProgress}
      fillOpacity={reveal.fillProgress * 0.55}
    />
    <div
      style={{
        position: "relative",
        height: "100%",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <span
        style={{
          fontFamily: FONT_STACK,
          fontSize,
          fontWeight: 800,
          color: COLORS.text,
          backgroundColor: COLORS.plate,
          padding: "6px 18px",
          borderRadius: 10,
        }}
      >
        {text}
      </span>
    </div>
  </div>
);
