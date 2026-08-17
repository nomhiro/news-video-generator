import { AbsoluteFill } from "remotion";
import { beatStart } from "../beats";
import { ChapterTag } from "../ChapterTag";
import { Subtitle } from "../Subtitle";
import { useZones } from "../zones";
import type { LayoutProps } from "../Video";
import { Headline } from "./Headline";

/**
 * 図なし。見出しだけを大きく見せる。フックと結論に使う。
 *
 * 要素の出現順は「章タグ → 見出し」（章が空文字列なら見出しのみ）。
 * 図が無いぶん、見出しのゾーンは `compare`/`flow` よりずっと広い
 * （`zones.ts` の `statement.headline` 参照）。
 */
export const Statement: React.FC<LayoutProps> = ({
  headline,
  subtitle,
  chapter,
  seed,
  durationInFrames,
}) => {
  const zones = useZones("statement");
  const hasChapter = chapter !== "";
  const totalBeats = hasChapter ? 2 : 1;
  const headlineStart = beatStart(hasChapter ? 1 : 0, totalBeats, durationInFrames);

  return (
    <AbsoluteFill>
      {hasChapter && (
        <ChapterTag
          text={chapter}
          seed={seed * 10 + 1}
          startFrame={beatStart(0, totalBeats, durationInFrames)}
          zone={zones.chapter}
        />
      )}
      <Headline text={headline} size={112} startFrame={headlineStart} zone={zones.headline} />
      <Subtitle text={subtitle} zone={zones.subtitle} />
    </AbsoluteFill>
  );
};
