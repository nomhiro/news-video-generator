import { useVideoConfig } from "remotion";
import { beatStart, useReveal } from "../beats";
import { ChapterTag } from "../ChapterTag";
import { Subtitle } from "../Subtitle";
import type { LayoutProps } from "../Video";
import { useZones } from "../zones";
import { Headline } from "./Headline";
import { RelationStrip } from "./RelationStrip";

/**
 * 原因 → 結果を、挿絵の下の1行ストリップで示す。
 *
 * 要素は「章タグ → 見出し → A → 矢印（＋関係ラベル） → B」の順に出る
 * （章が空なら見出しから）。以前は縦に並べた箱2つ＋縦の矢印で図の帯
 * （720px）いっぱいに描いていたが、その帯は共有の挿絵に譲った。1行の帯
 * （120px）には縦の矢印は収まらないため、`RelationStrip` は`Compare`と
 * 同じ水平のレイアウトを使い、`arrow: true` で方向（原因→結果）だけを
 * 描き分ける。
 */
export const Flow: React.FC<LayoutProps> = ({
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
  const totalBeats = (hasChapter ? 1 : 0) + 4; // 見出し・A・矢印・B
  let beat = 0;
  const chapterStart = hasChapter ? beatStart(beat++, totalBeats, durationInFrames) : 0;
  const headlineStart = beatStart(beat++, totalBeats, durationInFrames);
  const boxAStart = beatStart(beat++, totalBeats, durationInFrames);
  const arrowStart = beatStart(beat++, totalBeats, durationInFrames);
  const boxBStart = beatStart(beat++, totalBeats, durationInFrames);

  const boxA = useReveal(boxAStart);
  const boxB = useReveal(boxBStart);
  const arrowReveal = useReveal(arrowStart);

  return (
    <>
      {hasChapter && <ChapterTag text={chapter} startFrame={chapterStart} zone={zones.chapter} />}
      <Headline text={headline} startFrame={headlineStart} zone={zones.headline} />
      <RelationStrip
        items={items}
        relation={relation}
        // 因果は方向がある（原因→結果）ので矢印を付ける——`Compare` との違いはここだけ。
        arrow
        zone={zones.relation}
        frameWidth={width}
        boxA={boxA}
        boxB={boxB}
        relationReveal={arrowReveal}
      />
      <Subtitle text={subtitle} zone={zones.subtitle} />
    </>
  );
};
