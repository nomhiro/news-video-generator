/**
 * 色とフォントの単一の情報源。
 *
 * フォントは **Web フォントを使わない**。`fonts-noto-cjk`（本番イメージに
 * 既に入っている）を font-family で参照する。@font-face で非同期に読ませると、
 * delayRender / waitForFonts で待たない限り最初の数フレームだけ
 * フォールバックフォントで焼かれ、エラーにならないので気付きにくい。
 *
 * 代償: ローカル（Windows / Yu Gothic）と本番（Linux / Noto Sans CJK）で
 * 字形が変わる。最終確認は Docker 経由で行う。
 */
export const FONT_STACK =
  '"Noto Sans CJK JP", "Noto Sans JP", "Yu Gothic", "Meiryo", "Hiragino Sans", sans-serif';

export const COLORS = {
  bg: "#0b1020",
  text: "#ffffff",
  // 字幕は白文字。黄色＋黒ボックスをやめるのがこの作業の目的の1つ。
  subtle: "rgba(255,255,255,0.82)",
  accent: "#4cc9f0",
  accent2: "#f72585",
} as const;
