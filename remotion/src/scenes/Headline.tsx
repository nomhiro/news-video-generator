import { spring, useCurrentFrame, useVideoConfig } from "remotion";
import { COLORS, FONT_STACK } from "../theme";

/**
 * 1行に収まる文字数の見積り（全角、`size` が既定の 92px のとき）。
 *
 * 描画幅は 1080px から左右の padding（64〜72px）を引いた約 952px。
 * 92px の全角文字なら約10字。Latin は advance が半分程度なので、この
 * 見積りは英語では**過小**になる（英語は必要より早く縮む）。小さくなる方向は
 * 安全側なので、字幅の実測を持ち込むより単純さを取っている。
 */
const CHARS_PER_LINE_AT_BASE = 10;

/** 許す行数。これを超える見出しはフォントを縮めて詰める。 */
const MAX_LINES = 3;

/** 縮める下限（元サイズに対する比）。これ以上小さくすると見出しの役を果たさない。 */
const MIN_SCALE = 0.5;

/** 見出し。`Script.text_overlays[i]` が入る。 */
export const Headline: React.FC<{ text: string; size?: number }> = ({
  text,
  size = 92,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const enter = spring({ frame, fps, config: { damping: 200 } });

  // 長い見出しは字数に応じて縮める。
  //
  // 一次の防波堤は `script.MAX_HEADLINE_CHARS` のバリデータで、ここは保険。
  // `AbsoluteFill` はスクロールしないため、伸びた見出しは下の字幕スクリムに
  // 重なるか画面外に切れる。**壊れた文字列を「小さい文字」で済ませる**のが
  // ここの役割で、レイアウトの衝突より読みにくさの方が実害が小さい。
  const budget = CHARS_PER_LINE_AT_BASE * MAX_LINES;
  const scale = Math.max(MIN_SCALE, Math.min(1, budget / Math.max(text.length, 1)));
  const fontSize = size * scale;

  return (
    <h1
      style={{
        fontFamily: FONT_STACK,
        fontSize,
        fontWeight: 900,
        color: COLORS.text,
        textAlign: "center",
        lineHeight: 1.28,
        margin: 0,
        // 縮めても収まらない病的な入力に対する最後の歯止め。
        // 下の要素（compare の箱など）を押し下げて字幕に重ねるより、
        // 見出しが切れる方がまだ読める画になる。
        maxHeight: fontSize * 1.28 * MAX_LINES,
        overflow: "hidden",
        wordBreak: "auto-phrase",
        transform: `translateY(${(1 - enter) * 40}px)`,
        opacity: enter,
      }}
    >
      {text}
    </h1>
  );
};
