import { AbsoluteFill } from "remotion";
import { Headline } from "./Headline";

/** 図なし。見出しだけを大きく見せる。フックと結論に使う。 */
export const Statement: React.FC<{ headline: string; items: string[] }> = ({
  headline,
}) => (
  <AbsoluteFill
    style={{ justifyContent: "center", alignItems: "center", padding: 72 }}
  >
    <Headline text={headline} size={112} />
  </AbsoluteFill>
);
