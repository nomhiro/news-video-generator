import { spring, useCurrentFrame, useVideoConfig } from "remotion";
import { COLORS, FONT_STACK } from "../theme";

// Python 側（`src/utils/line_break.py`）が挿入する、日本語のフレーズ境界の
// ゼロ幅スペース。表示上は幅を持たないので、縮小率を決める文字数の計算からは
// 除外する必要がある（除外しないと、ZWSP の分だけ「文字数」が水増しされ、
// 見出しが必要以上に縮む——改行は直っても文字が小さくなる、という分かりにくい
// 退行になる）。
const ZWSP = "​";

/**
 * 1行に収まる文字数の見積り（全角、`size` が既定の 92px のとき）。
 *
 * 92px・幅952px の理論上の描画幅からは約10字/行と見積もれるが、これは
 * **実測と食い違っていた**。BudouX が挿入したフレーズ境界（ZWSP）で改行する
 * 実装では、行はフレーズの区切りでしか折れないため、理論上の文字数まで
 * 詰めることができず、常に手前で折れる。実際にレンダリングしたフレームで
 * 確かめたところ、18字の見出し（`推論コストが一桁下がる新しい学習方式`）は
 * 92px のまま3行に収まっているにもかかわらず、その改行は
 * 「推論コストが / 一桁下がる新しい / 学習方式」（6/8/4字）で、
 * どの行も8字を超えていなかった。フレーズ境界改行がある限り理論値は
 * 使えないので、実測の8を使う（Latin は advance が半分程度なので、この
 * 見積りは英語では**過小**になる。安全側なので単純さを取っている）。
 */
const CHARS_PER_LINE_AT_BASE = 8;

/** 許す行数。これを超える見出しはフォントを縮めて詰める。 */
const MAX_LINES = 3;

/** 縮める下限（元サイズに対する比）。これ以上小さくすると見出しの役を果たさない。 */
const MIN_SCALE = 0.5;

/** 見出し。`Script.text_overlays[i]` が入る。 */
export const Headline: React.FC<{ text: string; size?: number; startFrame?: number }> = ({
  text,
  size = 92,
  // ビート演出（chapter → headline → ...）に合わせて出現を遅らせるための
  // オフセット。既定の0は「シーン開始と同時に出る」旧来の挙動と一致する。
  startFrame = 0,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const enter = spring({ frame: frame - startFrame, fps, config: { damping: 200 } });

  // 長い見出しは字数に応じて縮める。
  //
  // 一次の防波堤は `script.MAX_HEADLINE_CHARS` のバリデータで、ここは保険。
  // `AbsoluteFill` はスクロールしないため、伸びた見出しは下の字幕スクリムに
  // 重なるか画面外に切れる。**壊れた文字列を「小さい文字」で済ませる**のが
  // ここの役割で、レイアウトの衝突より読みにくさの方が実害が小さい。
  const budget = CHARS_PER_LINE_AT_BASE * MAX_LINES;
  // ZWSP は表示幅0なので、縮小率の計算対象の文字数から除く（上の定数コメント参照）。
  const visibleLength = text.split(ZWSP).join("").length;
  const scale = Math.max(MIN_SCALE, Math.min(1, budget / Math.max(visibleLength, 1)));
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
        // `word-break: auto-phrase` を試したが実測で無効だった（Subtitle.tsx
        // 参照）。BudouX が挿入した ZWSP を良い改行点として使い、`keep-all` で
        // CJK 文字間の悪い改行点を禁止する。
        wordBreak: "keep-all",
        // ZWSP を挟んでも1フレーズが1行に収まらない病的な入力への安全弁。
        overflowWrap: "anywhere",
        transform: `translateY(${(1 - enter) * 40}px)`,
        opacity: enter,
      }}
    >
      {text}
    </h1>
  );
};
