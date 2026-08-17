import { AbsoluteFill } from "remotion";
import { beatStart, useReveal } from "../beats";
import { ChapterTag } from "../ChapterTag";
import { RoughConnector } from "../Rough";
import { COLORS, FONT_STACK } from "../theme";
import type { LayoutProps } from "../Video";
import { DiagramBox } from "./DiagramBox";
import { Headline } from "./Headline";

/**
 * 対比する2つを左右に並べる。
 *
 * 要素は「章タグ → 見出し → A → 関係ラベル → B」の順に出る（章が空なら
 * 見出しから）。一度に spring で出す旧実装は、図が「説明」ではなく
 * 「装飾」になってしまっていた。
 */
export const Compare: React.FC<LayoutProps> = ({
  headline,
  items,
  relation,
  chapter,
  seed,
  durationInFrames,
}) => {
  const hasChapter = chapter !== "";
  const totalBeats = (hasChapter ? 1 : 0) + 4; // 見出し・A・関係・B
  let beat = 0;
  const chapterStart = hasChapter ? beatStart(beat++, totalBeats, durationInFrames) : 0;
  const headlineStart = beatStart(beat++, totalBeats, durationInFrames);
  const boxAStart = beatStart(beat++, totalBeats, durationInFrames);
  const relationStart = beatStart(beat++, totalBeats, durationInFrames);
  const boxBStart = beatStart(beat++, totalBeats, durationInFrames);

  const boxA = useReveal(boxAStart);
  const boxB = useReveal(boxBStart);
  const relationReveal = useReveal(relationStart);

  return (
    <AbsoluteFill
      style={{ justifyContent: "center", alignItems: "center", padding: 64 }}
    >
      {hasChapter && (
        <ChapterTag text={chapter} seed={seed * 10 + 1} startFrame={chapterStart} />
      )}
      <Headline text={headline} startFrame={headlineStart} />
      <div style={{ display: "flex", alignItems: "center", gap: 40, marginTop: 88 }}>
        {/* items は schema が常に2要素であることを保証しているので固定 index。 */}
        <DiagramBox
          text={items[0]}
          width={380}
          height={380}
          color={COLORS.accent}
          seed={seed * 10 + 2}
          reveal={boxA}
        />
        <div
          style={{ position: "relative", width: 96, height: 40, opacity: relationReveal.opacity }}
        >
          <RoughConnector
            width={96}
            height={40}
            seed={seed * 10 + 3}
            stroke={COLORS.subtle}
            orientation="horizontal"
            arrow={false}
            drawProgress={relationReveal.drawProgress}
          />
          {/* 線の真上ではなく上に離して置く（線とラベルが重なっていた不具合）。 */}
          <span
            style={{
              position: "absolute",
              top: -34,
              left: "50%",
              transform: "translateX(-50%)",
              fontFamily: FONT_STACK,
              fontSize: 30,
              fontWeight: 700,
              color: COLORS.text,
              backgroundColor: COLORS.plate,
              padding: "2px 10px",
              borderRadius: 6,
              whiteSpace: "nowrap",
            }}
          >
            {relation}
          </span>
        </div>
        <DiagramBox
          text={items[1]}
          width={380}
          height={380}
          color={COLORS.accent2}
          seed={seed * 10 + 4}
          reveal={boxB}
        />
      </div>
    </AbsoluteFill>
  );
};
