import { AbsoluteFill } from "remotion";
import { beatStart } from "../beats";
import { ChapterTag } from "../ChapterTag";
import type { LayoutProps } from "../Video";
import { Headline } from "./Headline";

/**
 * 図なし。見出しだけを大きく見せる。フックと結論に使う。
 *
 * 要素の出現順は「章タグ → 見出し」（章が空文字列なら見出しのみ）。
 */
export const Statement: React.FC<LayoutProps> = ({
  headline,
  chapter,
  seed,
  durationInFrames,
}) => {
  const hasChapter = chapter !== "";
  const totalBeats = hasChapter ? 2 : 1;
  const headlineStart = beatStart(hasChapter ? 1 : 0, totalBeats, durationInFrames);

  return (
    <AbsoluteFill
      style={{ justifyContent: "center", alignItems: "center", padding: 72 }}
    >
      {hasChapter && (
        <ChapterTag
          text={chapter}
          seed={seed * 10 + 1}
          startFrame={beatStart(0, totalBeats, durationInFrames)}
        />
      )}
      <Headline text={headline} size={112} startFrame={headlineStart} />
    </AbsoluteFill>
  );
};
