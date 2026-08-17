import { useVideoConfig } from "remotion";

export type Zone = { top: number; height: number };

/**
 * 縦方向のレイアウトを「ゾーン」として一箇所で定義する。
 *
 * **共有の挿絵が上半分（52%）を占める構成に変えた。** 手描きの箱2つで
 * 対比/因果を説明していた版は「地味で観られない」というオーナーの判断で
 * 退役し、代わりに動画全体で共有する1枚の挿絵を主役に据える。
 * 図（箱・矢印）が持っていた「概念を示す」役割は、挿絵の下の
 * 120px の1行ストリップ（`RelationStrip`）に圧縮して引き継ぐ。
 *
 * ゾーンの重なり防止という設計そのものは変えていない——各要素は自分の
 * ゾーンの内側にしか描かない。
 *
 * 比率（0〜1、フレーム高さに対する割合）で持つ理由は変わらず、ピクセル
 * 固定だと長尺（1920x1080）でそのまま使ったときに範囲外に出るため。
 * 数値は縦画面（1920 高）の実フレームを見て決めている（下記コメント参照）。
 */
const RATIOS = {
  // 挿絵と章タグは compare/flow/statement の3レイアウトで共通。
  // 挿絵自体の描画は `Video.tsx` がシーケンスの外（トップレベル）で行うが、
  // 「挿絵がどこまでの帯を占めるか」は各シーンも知る必要がある
  // （章タグをその帯の下端に置くため）。
  shared: {
    // 挿絵の帯。フレームの上48%、四辺フルブリードで描く。
    //
    // **当初 52%（1000px）で出していたが、`strip` レイアウトの見出しゾーン
    // （320px）で45字の見出しが3字（`底解説`）欠けた（実測）。** 見出しの
    // 縮小係数（`Headline.tsx` の `LINE_ESTIMATE_SAFETY_MARGIN`）を追いかけて
    // 通すのは、実測に対して2度も外れた係数をさらに信用することになるので
    // 選ばない——**帯側に余裕を持たせる**方針にし、挿絵から80px 借りて
    // 見出しに渡す。挿絵は 48% でも十分に主役として見える（帯の高さでは
    // なく「四辺フルブリード＋ドリフト」が主役感を作っている）。
    illustration: { top: 0, height: 920 / 1920 },
    // 章タグはこの帯（挿絵の下端寄り180px）の中に左寄せで置く。
    // 全幅ではなく挿絵の中に重ねるので、独立したゾーンとして持つ。
    chapter: { top: 740 / 1920, height: 180 / 1920 },
  },
  // compare/flow のゾーン。挿絵の下に見出し・関係ストリップ・字幕が続く。
  strip: {
    // 320→400。上のコメント参照——45字の見出しが4行しか使えず欠けた
    // （実測）。下端（1370）は変えていないので、関係ストリップ・字幕の
    // 位置はそのまま。
    headline: { top: 970 / 1920, height: 400 / 1920 },
    // 対比/因果の2要素を1行で示すストリップ。図（箱・矢印）が退役した後も
    // `items` / `relation` はスキーマ上必須のままなので、描く場所を残す
    // （LLM に出させて誰も描かないデータを放置すると腐る）。
    relation: { top: 1410 / 1920, height: 120 / 1920 },
    subtitle: { top: 1570 / 1920, height: 350 / 1920 },
  },
  // statement には items/relation が無い（契約上つねに空）ので、
  // ストリップとその前後の余白を見出しに譲る。
  statement: {
    headline: { top: 1050 / 1920, height: 520 / 1920 },
    subtitle: { top: 1570 / 1920, height: 350 / 1920 },
  },
} as const;

function toPx(ratio: { top: number; height: number }, frameHeight: number): Zone {
  return { top: ratio.top * frameHeight, height: ratio.height * frameHeight };
}

/** 3レイアウトに共通のゾーン。 */
export type Zones = { illustration: Zone; chapter: Zone; headline: Zone; subtitle: Zone };

/** `compare` / `flow` のゾーン。関係ストリップの帯を必ず持つ。 */
export type StripZones = Zones & { relation: Zone };

/**
 * `layout` に応じたゾーンをまとめて返す。
 *
 * **返り値の型を overload で分ける。** 単一の型で `relation?: Zone` を返すと、
 * ストリップを持つレイアウト側が `zones.relation!` と書くことになり、
 * 「ストリップの帯が必ずある」ことを型で言えなくなる（`!` は将来ゾーンの構成を
 * 変えたときに実行時エラーへ化ける）。overload なら
 * `useZones("strip").relation` が `Zone` として通る。
 *
 * `useVideoConfig()` は分岐の外で1回だけ呼ぶ（Hooks のルール）。分岐は
 * その結果（`height`）を使ったプレーンな JS の分岐であり、呼ぶフック自体を
 * 切り替えているわけではない。
 */
export function useZones(layout: "strip"): StripZones;
export function useZones(layout: "statement"): Zones;
export function useZones(layout: "strip" | "statement"): Zones | StripZones {
  const { height } = useVideoConfig();
  const ratios = RATIOS[layout];
  const common: Zones = {
    illustration: toPx(RATIOS.shared.illustration, height),
    chapter: toPx(RATIOS.shared.chapter, height),
    headline: toPx(ratios.headline, height),
    subtitle: toPx(ratios.subtitle, height),
  };
  if (layout === "statement") return common;
  return { ...common, relation: toPx(RATIOS.strip.relation, height) };
}
