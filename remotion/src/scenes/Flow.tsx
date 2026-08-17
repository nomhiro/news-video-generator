import { useVideoConfig } from "remotion";
import { beatStart, useReveal } from "../beats";
import { ChapterTag } from "../ChapterTag";
import { fitFontSize } from "../fitText";
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
/** 矢印の帯の幅。ラベルはこの右側に置く。 */
const ARROW_WIDTH = 220;
/**
 * 関係ラベルの上限サイズ。
 *
 * 縦並びの `flow` は矢印の右に横幅が丸ごと余っているので、`compare` より
 * 大きくできる。**関係が図の主張**であって箱の付属物ではない。
 */
const RELATION_MAX_SIZE = 56;
/** 関係ラベルのプレートの左右パディング。 */
const RELATION_PLATE_PADDING_X = 16;
/** 矢印の右端からラベルまでの距離。 */
const RELATION_OFFSET_X = 24;

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
  // ラベルは矢印の右端から画面右の余白までに収める。プレートのパディングと
  // 矢印からの距離を先に引く（引き忘れるとプレートが画面外にはみ出す）。
  const relationSize = fitFontSize(
    relation,
    (width - ARROW_WIDTH) / 2 - SIDE_PADDING - RELATION_OFFSET_X - RELATION_PLATE_PADDING_X * 2,
    RELATION_MAX_SIZE,
  );

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
          style={{
            position: "relative",
            width: ARROW_WIDTH,
            height: ARROW_HEIGHT,
            opacity: arrow.opacity,
          }}
        >
          <RoughConnector
            width={ARROW_WIDTH}
            height={ARROW_HEIGHT}
            seed={seed * 10 + 3}
            stroke={COLORS.subtle}
            orientation="vertical"
            arrow
            drawProgress={arrow.drawProgress}
          />
          {/* 矢印線の右に離して置く（以前は線の真上に重なっていた不具合）。
              `left` は矢印の右端の少し外側。割合ではなく px で置くのは、
              文字を大きくしても線に近づかないようにするため。 */}
          <span
            style={{
              position: "absolute",
              top: "50%",
              left: ARROW_WIDTH / 2 + RELATION_OFFSET_X,
              transform: "translateY(-50%)",
              fontFamily: FONT_STACK,
              fontSize: relationSize,
              fontWeight: 900,
              color: COLORS.text,
              backgroundColor: COLORS.plate,
              padding: `4px ${RELATION_PLATE_PADDING_X}px`,
              borderRadius: 8,
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
