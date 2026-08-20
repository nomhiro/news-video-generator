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
