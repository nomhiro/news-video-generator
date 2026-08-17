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

/** コネクタ（横線）の帯の幅。関係ラベルを線の上に離して置く余白を含む。 */
const CONNECTOR_WIDTH = 100;
/** 箱どうし・箱とコネクタの間の隙間。 */
const GAP = 32;
/** 図のゾーンの左右に残す余白。 */
const SIDE_PADDING = 64;
/** 箱の縦の余白（ラベルが枠にぶつからないよう少し引く）。 */
const BOX_MARGIN = 24;

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

  const boxWidth = (width - SIDE_PADDING * 2 - CONNECTOR_WIDTH - GAP * 2) / 2;
  const boxHeight = zones.diagram.height - BOX_MARGIN * 2;
  const connectorHeight = 40;

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
        />
        <div
          style={{
            position: "relative",
            width: CONNECTOR_WIDTH,
            height: connectorHeight,
            opacity: relationReveal.opacity,
          }}
        >
          <RoughConnector
            width={CONNECTOR_WIDTH}
            height={connectorHeight}
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
          width={boxWidth}
          height={boxHeight}
          color={COLORS.accent2}
          seed={seed * 10 + 4}
          reveal={boxB}
        />
      </div>
      <Subtitle text={subtitle} zone={zones.subtitle} />
    </>
  );
};
