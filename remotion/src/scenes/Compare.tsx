import { useVideoConfig } from "remotion";
import { beatStart, useReveal } from "../beats";
import { ChapterTag } from "../ChapterTag";
import { fitFontSize } from "../fitText";
import { RoughConnector } from "../Rough";
import { Subtitle } from "../Subtitle";
import { COLORS, FONT_STACK } from "../theme";
import type { LayoutProps } from "../Video";
import { useZones } from "../zones";
import { DEFAULT_LABEL_SIZE, DiagramBox, LABEL_INSET } from "./DiagramBox";
import { Headline } from "./Headline";

/**
 * 関係ラベルの列の幅。
 *
 * 100px から広げた。**関係こそが図の主張**で、箱はその両端に過ぎないのに、
 * 大きなハッチングの箱2つに挟まれた 30px のラベルは付け足しに見えていた
 * （実測）。ラベルに専用の幅を与え、そのぶん箱を狭める。
 */
const RELATION_WIDTH = 220;
/** 箱どうし・箱と関係ラベルの間の隙間。 */
const GAP = 32;
/** 図のゾーンの左右に残す余白。 */
const SIDE_PADDING = 48;
/** 箱の縦の余白（ラベルが枠にぶつからないよう少し引く）。 */
const BOX_MARGIN = 24;
/** 関係ラベルの上限サイズ。実際は文字数から下に丸める。 */
const RELATION_MAX_SIZE = 52;
/** 関係ラベルのプレートの左右パディング。 */
const RELATION_PLATE_PADDING_X = 16;

/**
 * 対比する2つを左右に並べる。
 *
 * 要素は「章タグ → 見出し → A → 関係ラベル → B」の順に出る（章が空なら
 * 見出しから）。一度に spring で出す旧実装は、図が「説明」ではなく
 * 「装飾」になってしまっていた。
 *
 * 図の大きさは `zones.diagram` から逆算する。以前は 380x380 の固定値で、
 * 見出しと字幕の間に余っていた空間を使っていなかった
 * （ゾーン導入前は誰もその空間の所有者ではなかった）。
 */
export const Compare: React.FC<LayoutProps> = ({
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

  const boxWidth = (width - SIDE_PADDING * 2 - RELATION_WIDTH - GAP * 2) / 2;
  const boxHeight = zones.diagram.height - BOX_MARGIN * 2;
  // **2つの箱のラベルは同じサイズにする。** 箱ごとに文字数から独立に決めると、
  // `従来モデル`（5字→53px）と `新方式`（3字→84px）のように左右で
  // 大きさが揃わず、対比すべき2つが対等に見えない（実測）。
  // 短い側を長い側に合わせる（小さい方を採る）。
  const labelCap = Math.min(
    ...items.map((item) => fitFontSize(item, boxWidth - LABEL_INSET, DEFAULT_LABEL_SIZE)),
  );
  const connectorHeight = 40;
  // ラベルは左右の GAP にはみ出してよいが、**箱の内側には入らない**。
  // プレートのパディングを引いてから文字幅に配ること。引き忘れると
  // プレートが箱に重なる（8文字の実測で両側の箱に重なった）。
  // GAP は片側ぶんだけ使い、残り半分は箱との間の空きとして残す。
  const relationSize = fitFontSize(
    relation,
    RELATION_WIDTH + GAP - RELATION_PLATE_PADDING_X * 2,
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
          top: zones.diagram.top + BOX_MARGIN,
          left: 0,
          right: 0,
          height: boxHeight,
          display: "flex",
          justifyContent: "center",
          alignItems: "center",
          gap: GAP,
        }}
      >
        {/* items は schema が常に2要素であることを保証しているので固定 index。 */}
        <DiagramBox
          text={items[0]}
          width={boxWidth}
          height={boxHeight}
          color={COLORS.accent}
          seed={seed * 10 + 2}
          reveal={boxA}
          fontSize={labelCap}
        />
        <div
          style={{
            position: "relative",
            width: RELATION_WIDTH,
            height: connectorHeight,
            opacity: relationReveal.opacity,
          }}
        >
          <RoughConnector
            width={RELATION_WIDTH}
            height={connectorHeight}
            seed={seed * 10 + 3}
            stroke={COLORS.subtle}
            orientation="horizontal"
            arrow={false}
            drawProgress={relationReveal.drawProgress}
          />
          {/* 線の真上ではなく上に離して置く（線とラベルが重なっていた不具合）。
              離す距離はサイズに比例させる。固定値だと大きくした文字が線に
              かぶる。 */}
          <span
            style={{
              position: "absolute",
              top: -relationSize * 1.15,
              left: "50%",
              transform: "translateX(-50%)",
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
          fontSize={labelCap}
        />
      </div>
      <Subtitle text={subtitle} zone={zones.subtitle} />
    </>
  );
};
