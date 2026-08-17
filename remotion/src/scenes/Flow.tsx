import { useVideoConfig } from "remotion";
import { beatStart, useReveal } from "../beats";
import { ChapterTag } from "../ChapterTag";
import { RoughConnector } from "../Rough";
import { Subtitle } from "../Subtitle";
import { COLORS, FONT_STACK } from "../theme";
import type { LayoutProps } from "../Video";
import { useZones } from "../zones";
import { DiagramBox } from "./DiagramBox";
import { Headline } from "./Headline";

/** 矢印の帯の高さ。関係ラベルを矢印の横に離して置く余白を含む。 */
const ARROW_HEIGHT = 130;
/** 図のゾーンの左右に残す余白。 */
const SIDE_PADDING = 64;

/**
 * 原因 → 結果を矢印で繋ぐ。上から下に流す（縦画面なので縦に並べる）。
 *
 * 要素は「章タグ → 見出し → A → 矢印（＋関係ラベル） → B」の順に出る
 * （章が空なら見出しから）。図の大きさは `zones.diagram` から逆算する
 * （`Compare.tsx` と同じ理由）。
 */
export const Flow: React.FC<LayoutProps> = ({
  headline,
  subtitle,
  items,
  relation,
  chapter,
  seed,
  durationInFrames,
}) => {
  const { width } = useVideoConfig();
  const zones = useZones("diagram");

  const hasChapter = chapter !== "";
  const totalBeats = (hasChapter ? 1 : 0) + 4; // 見出し・A・矢印・B
  let beat = 0;
  const chapterStart = hasChapter ? beatStart(beat++, totalBeats, durationInFrames) : 0;
  const headlineStart = beatStart(beat++, totalBeats, durationInFrames);
  const boxAStart = beatStart(beat++, totalBeats, durationInFrames);
  const arrowStart = beatStart(beat++, totalBeats, durationInFrames);
  const boxBStart = beatStart(beat++, totalBeats, durationInFrames);

  const boxA = useReveal(boxAStart);
  const boxB = useReveal(boxBStart);
  const arrow = useReveal(arrowStart);

  const boxWidth = width - SIDE_PADDING * 2;
  const boxHeight = (zones.diagram.height - ARROW_HEIGHT) / 2;

  return (
    <>
      {hasChapter && (
        <ChapterTag text={chapter} seed={seed * 10 + 1} startFrame={chapterStart} zone={zones.chapter} />
      )}
      <Headline text={headline} startFrame={headlineStart} zone={zones.headline} />
      <div
        style={{
          position: "absolute",
          top: zones.diagram.top,
          left: 0,
          right: 0,
          height: zones.diagram.height,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
        }}
      >
        <DiagramBox
          text={items[0]}
          width={boxWidth}
          height={boxHeight}
          color={COLORS.accent}
          seed={seed * 10 + 2}
          reveal={boxA}
          fontSize={76}
        />
        <div
          style={{ position: "relative", width: 220, height: ARROW_HEIGHT, opacity: arrow.opacity }}
        >
          <RoughConnector
            width={220}
            height={ARROW_HEIGHT}
            seed={seed * 10 + 3}
            stroke={COLORS.subtle}
            orientation="vertical"
            arrow
            drawProgress={arrow.drawProgress}
          />
          {/* 矢印線の右に離して置く（以前は線の真上に重なっていた不具合）。 */}
          <span
            style={{
              position: "absolute",
              top: "50%",
              left: "64%",
              transform: "translateY(-50%)",
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
          width={boxWidth}
          height={boxHeight}
          color={COLORS.accent2}
          seed={seed * 10 + 4}
          reveal={boxB}
          fontSize={76}
        />
      </div>
      <Subtitle text={subtitle} zone={zones.subtitle} />
    </>
  );
};
