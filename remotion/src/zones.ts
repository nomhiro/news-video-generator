import { useVideoConfig } from "remotion";

export type Zone = { top: number; height: number };

/**
 * 縦方向のレイアウトを「ゾーン」として一箇所で定義する。
 *
 * 章タグ・見出し・図・字幕をそれぞれ独立に位置決めしていたときは、
 * 「上1/3が空く」「字幕が図の下端と重なる」のように、誰も責任を持たない
 * 隙間と重なりが両方発生し得た（実測で両方確認した）。ゾーンの高さの
 * 合計でフレームの縦を分け、各要素は自分のゾーンの内側にしか描かないと
 * 決めれば、重なりは構造的に起こらず、余白も意図した配分になる。
 *
 * 比率（0〜1、フレーム高さに対する割合）で持つ。ピクセル固定にすると
 * 長尺（1920x1080、横長）でそのまま使ったときに範囲がフレーム外に出る。
 * 数値そのものは縦画面（1920 高）でレンダリングした実フレームを見て
 * 決めている（下記コメント参照）。長尺のような別アスペクト比では
 * 同じ比率が最適とは限らないが、現状「長尺は当面作らない」
 * （CLAUDE.md）ため優先していない。
 */
const RATIOS = {
  diagram: {
    // 章タグ。3レイアウト共通の帯。
    chapter: { top: 120 / 1920, height: 140 / 1920 },
    // compare/flow の見出し。図が下に控えている分、statement より狭い。
    headline: { top: 300 / 1920, height: 380 / 1920 },
    // compare/flow の図。以前は見出しと字幕の間の余白に埋もれていた領域を
    // ここに明示的に割り当てる——スマホでは図が大きい方が読める。
    diagram: { top: 720 / 1920, height: 620 / 1920 },
    // 字幕は**フレーム下端まで**を持つ。スクリム（下端のグラデーション）は
    // 必ず下端まで伸びるので、ここに 340 のような「実際に描く範囲より
    // 狭い高さ」を書くと、ゾーンの値がレイアウトの実態を表さなくなる
    // （ゾーンで重なりを防ぐという仕組みが、値を信じられない時点で崩れる）。
    subtitle: { top: 1400 / 1920, height: 520 / 1920 },
  },
  // statement には図が無いので、見出しがフレームのほぼ全体を持つ。
  statement: {
    chapter: { top: 120 / 1920, height: 140 / 1920 },
    headline: { top: 320 / 1920, height: 1080 / 1920 },
    subtitle: { top: 1460 / 1920, height: 460 / 1920 },
  },
} as const;

function toPx(ratio: { top: number; height: number }, frameHeight: number): Zone {
  return { top: ratio.top * frameHeight, height: ratio.height * frameHeight };
}

/** 3レイアウトに共通のゾーン。 */
export type Zones = { chapter: Zone; headline: Zone; subtitle: Zone };

/** `compare` / `flow` のゾーン。図の帯を必ず持つ。 */
export type DiagramZones = Zones & { diagram: Zone };

/**
 * `layout` に応じたゾーンをまとめて返す。
 *
 * **返り値の型を overload で分ける。** 単一の型で `diagram?: Zone` を返すと、
 * 図を持つレイアウト側が `zones.diagram!` と書くことになり、
 * 「図の帯が必ずある」ことを型で言えなくなる（`!` は将来ゾーンの構成を
 * 変えたときに実行時エラーへ化ける）。overload なら
 * `useZones("diagram").diagram` が `Zone` として通る。
 *
 * `useVideoConfig()` は分岐の外で1回だけ呼ぶ（Hooks のルール）。分岐は
 * その結果（`height`）を使ったプレーンな JS の分岐であり、呼ぶフック自体を
 * 切り替えているわけではない。
 */
export function useZones(layout: "diagram"): DiagramZones;
export function useZones(layout: "statement"): Zones;
export function useZones(layout: "diagram" | "statement"): Zones | DiagramZones {
  const { height } = useVideoConfig();
  const ratios = RATIOS[layout];
  const common: Zones = {
    chapter: toPx(ratios.chapter, height),
    headline: toPx(ratios.headline, height),
    subtitle: toPx(ratios.subtitle, height),
  };
  if (layout === "statement") return common;
  return { ...common, diagram: toPx(RATIOS.diagram.diagram, height) };
}
