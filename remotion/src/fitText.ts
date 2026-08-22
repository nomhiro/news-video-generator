/**
 * 「1行に収める」ためのフォントサイズを文字数と使える幅から決める。
 *
 * なぜ折り返しではなく縮小か
 * --------------------------
 * 日本語には単語の境界が無いため、箱のラベルのような短い語が折り返すと
 * `従来モデル` が `従来モデ` / `ル` のように**語の途中で割れる**
 * （実測で確認した不具合）。ラベルは図の名札であって文章ではないので、
 * 割れるより小さくなる方が読める。見出し（`Headline`）が縦のゾーンから
 * サイズを逆算しているのと同じ考え方を、横幅に対して適用する。
 */

/**
 * 全角1文字の送り幅（フォントサイズに対する比）。
 *
 * CJK の全角は概ね 1em。半角の数字や英字は 0.5em 程度なので、1.0 で見積もると
 * `1/10` のような文字列では**余る側に外れる**（`fitFontSize` は上限も取るので
 * 余った場合は上限で止まる）。足りない側に外れると1行に収まらず、
 * 縮小の目的そのものが崩れるため、意図して保守側の値にしている。
 */
const FULL_WIDTH_ADVANCE = 1.0;

/**
 * `text` を1行で `availableWidth` に収めるフォントサイズを返す。
 *
 * @param text 収めたい文字列
 * @param availableWidth 使える幅（px）。パディングは呼び出し側で引いておく
 * @param maxFontSize 上限（px）。短い文字列を無闇に大きくしないため
 */
export function fitFontSize(text: string, availableWidth: number, maxFontSize: number): number {
  const chars = Math.max(text.length, 1);
  return Math.min(maxFontSize, availableWidth / (chars * FULL_WIDTH_ADVANCE));
}

/**
 * ゼロ幅スペース（U+200B）。Python 側（`src/utils/line_break.py`）が BudouX の
 * フレーズ境界に挿入する。表示幅を持たないので、幅の見積りからは除外しなければ
 * ならない（除外しないと ZWSP のぶん幅が水増しされ、必要以上に小さく描かれる）。
 */
const ZWSP = "​";

/** ASCII・ラテン文字の送り幅（em）。CJK の全角は 1.0。 */
const ASCII_ADVANCE = 0.55;

/**
 * 全角換算の合計幅（em）を見積もる。
 *
 * **`fitFontSize` の「文字数 × 1.0」では字幕には粗すぎる。** ラベル（8字以内）
 * では ASCII が混じっても誤差が小さく、しかも安全側（余る側）に外れるので 1.0 で
 * 足りていた。字幕は40字前後あり、`Claude` / `Anthropic` / `サイレントAIユーザー`
 * のように ASCII を含む語が実際に頻出する。実測フレームで
 * `Claudeの文章に、目に見えない`（14字）は 936px 幅のうち約772px を占めており、
 * 1.0 で数えた 14em = 756px とほぼ一致する——一方 `Anthropic`（9字）を 9em = 486px
 * と見積もるのは実態（約270px）の1.8倍で、**行数を過大に見積もって字幕が不必要に
 * 縮む**。
 *
 * 0.55em は Noto Sans CJK Bold / Yu Gothic Bold の実測に基づく近似。
 * `video_composer._line_width` が Pillow で厳密に測っているのと同じ問題を、
 * ブラウザに幅を問えない側から近似で解いている。
 */
export function estimateEmWidth(text: string): number {
  let em = 0;
  for (const ch of text) {
    if (ch === ZWSP) continue;
    // U+2E80 未満は ASCII・ラテン拡張・一般記号。CJK・かな・全角記号は 1em。
    em += (ch.codePointAt(0) ?? 0) < 0x2e80 ? ASCII_ADVANCE : 1.0;
  }
  return em;
}
