import { useVideoConfig } from "remotion";
import { beatStart, useReveal } from "../beats";
import { ChapterTag } from "../ChapterTag";
import { Subtitle } from "../Subtitle";
import type { LayoutProps } from "../Video";
import { useZones } from "../zones";
import { Headline } from "./Headline";
import { RelationStrip } from "./RelationStrip";

/**
 * 対比する2つを、挿絵の下の1行ストリップで示す。
 *
 * 要素は「章タグ → 見出し → A → 関係ラベル → B」の順に出る（章が空なら
 * 見出しから）。以前は箱2つ＋関係ラベルを図の帯（720px）いっぱいに描いて
 * いたが、その帯は共有の挿絵に譲った（`zones.ts` 参照）。概念は
 * `RelationStrip`（120px）に圧縮して引き継ぐ。
 */
export const Compare: React.FC<LayoutProps> = ({
  headline,
  subtitle,
  items,
  relation,
  chapter,
  durationInFrames,
}) => {
  const { width } = useVideoConfig();
  const zones = useZones("strip");

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
    <>
      {hasChapter && <ChapterTag text={chapter} startFrame={chapterStart} zone={zones.chapter} />}
      <Headline text={headline} startFrame={headlineStart} zone={zones.headline} />
      <RelationStrip
        items={items}
        relation={relation}
        // 対比は対称（方向が無い）ので矢印は付けない——`Flow` との違いはここだけ。
        arrow={false}
        zone={zones.relation}
        frameWidth={width}
        boxA={boxA}
        boxB={boxB}
        relationReveal={relationReveal}
      />
      <Subtitle text={subtitle} zone={zones.subtitle} />
    </>
  );
};
