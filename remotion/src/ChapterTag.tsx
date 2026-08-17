import { useReveal } from "./beats";
import { RoughRect } from "./Rough";
import { COLORS, FONT_STACK } from "./theme";

/**
 * 画面上部の小さな手描きタグ。`chapter`（例: "仕組み"）を表示する。
 *
 * 全シーンが縦中央寄せのため、画面の上1/3は恒常的に空いている。
 * ここを埋めつつ、「いま構成のどの段（フック/事実/仕組み/インパクト/結論）
 * にいるか」を視聴者に示す。
 *
 * `chapter` が空文字列のときは**何も描かない**。Python 側が事情により
 * ラベルを付けられない場合に空文字列で「劣化」させて渡す契約になっている
 * （失敗させるのではなく、タグ無しで動画は成立させる）。
 */
export const ChapterTag: React.FC<{ text: string; seed: number; startFrame: number }> = ({
  text,
  seed,
  startFrame,
}) => {
  const { drawProgress, fillProgress, opacity } = useReveal(startFrame);
  if (!text) return null;

  const width = 64 + text.length * 34;
  const height = 76;

  return (
    <div
      style={{
        position: "absolute",
        top: 96,
        left: "50%",
        transform: "translateX(-50%)",
        width,
        height,
        opacity,
      }}
    >
      <RoughRect
        width={width}
        height={height}
        seed={seed}
        stroke={COLORS.accent}
        fill={COLORS.accent}
        drawProgress={drawProgress}
        fillOpacity={fillProgress * 0.5}
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
        {/* ハッチング塗りの上に文字を直接置くと斜線と字がぶつかる
            （Compare/Flow の箱ラベルと同じ問題）。地の色に近い小さな
            プレートを文字の背後に敷いて、塗りをそこだけ避ける。 */}
        <span
          style={{
            fontFamily: FONT_STACK,
            fontSize: 34,
            fontWeight: 700,
            color: COLORS.text,
            backgroundColor: COLORS.plate,
            padding: "4px 14px",
            borderRadius: 6,
          }}
        >
          {text}
        </span>
      </div>
    </div>
  );
};
