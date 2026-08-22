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
 *
 * **下端の 150px はプラットフォームの UI に明け渡してある**（`SAFE_BOTTOM`）。
 * 2026-08-23 に章タグより下の数値がまとめて上へ動いた理由がこれ。
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
    //
    // **920 → 800（2026-08-20）。挿絵を白地の紙のカードにしたため。**
    // 章タグを絵の上に重ねるのをやめ、絵の下の暗い地に置く場所を作った。
    // 白地では重なりが目立つので、フェードに沈めて被害を減らすのではなく、
    // 重なりうる構造そのものを無くす。
    illustration: { top: 0, height: 800 / 1920 },
    // 章タグは挿絵カードの**すぐ下**の暗い地に左寄せで置く（800〜896px）。
    //
    // **以前は挿絵に重ねていた。** 実際に生成した挿絵で章タグが円形アイコンの
    // 1つに重なって隠していたため、下端フェードが最も強く効く範囲まで沈めて
    // 被害を減らしていた。**挿絵を白地にした時点でこの妥協は成立しなくなった**
    // ——白地の上では重なりが明確に見え、フェードも白を暗地へ滲ませるだけに
    // なる。挿絵の帯を 920→800 に縮め、タグを絵の外へ出した。
    // 「文字を置く場所」と「絵の被写体」が独立に決まる問題は、重ねるのを
    // やめれば起こりえない。
    //
    // **120 → 96（2026-08-23）。** 字幕を UI の帯（`SAFE_BOTTOM`）から
    // 逃がすために字幕ゾーンを 90px 上へ伸ばした。その 90px は**挿絵の帯
    // ではなく、この帯とゾーン間の余白から出している**——挿絵の帯を縮めると
    // アスペクト比が変わり、`ILLUSTRATION_SIZE`（画像生成に渡すサイズ、
    // `src/generators/remotion_renderer.py`）の見直しと画像の再検証まで
    // 連鎖する（クォータはリージョン単位で上限4、動画生成と共食いする）。
    // 章タグの高さは 76px 固定（`ChapterTag.tsx`）なので、96px の帯でも
    // 上下に 10px 残る。**76px を下回るところまでは縮められない。**
    chapter: { top: 800 / 1920, height: 96 / 1920 },
  },
  // compare/flow のゾーン。挿絵の下に見出し・関係ストリップ・字幕が続く。
  strip: {
    // 320→400。上のコメント参照——45字の見出しが4行しか使えず欠けた
    // （実測）。
    //
    // **高さ 400 は変えずに 50px 上へ動かした（2026-08-23）。** 高さを削って
    // 場所を作るのは筋が違う——`Headline.tsx` はフォントサイズを
    // `zone.height` から逆算するので、削れば 320→400 で直した「45字が欠ける」
    // 側へ戻る方向に効く。
    headline: { top: 920 / 1920, height: 400 / 1920 },
    // 対比/因果の2要素を1行で示すストリップ。図（箱・矢印）が退役した後も
    // `items` / `relation` はスキーマ上必須のままなので、描く場所を残す
    // （LLM に出させて誰も描かないデータを放置すると腐る）。
    //
    // **1410 → 1340（2026-08-23）。高さ 120 は変えない。** `RelationStrip` の
    // プレートは「下端を線から離す」基準で置き、上端がゾーン上端の 8px 下に
    // 収まることを計算で言えるようにしてある（`RelationStrip.tsx` の
    // `RELATION_MAX_SIZE`）。縮めるとその 8px が消える。
    relation: { top: 1340 / 1920, height: 120 / 1920 },
    // **350 → 440 / 上端 1570 → 1480（2026-08-23）。テキスト領域は 266px の
    // まま**（440 − 12 − 162 = 266 = 350 − 12 − 72）。字幕は下端寄せなので、
    // 下パディングを UI のぶん増やした量だけゾーンを上へ伸ばせば、
    // `fitSubtitleSize` に渡る高さが変わらない——「基準サイズ 46px で4行が
    // 収まる」という不変条件（`tests/test_remotion_design_rules.py`）に
    // 触らずに、文字だけを UI の上へ持ち上げられる。
    subtitle: { top: 1480 / 1920, height: 440 / 1920 },
  },
  // statement には items/relation が無い（契約上つねに空）ので、
  // ストリップとその前後の余白を見出しに譲る。
  statement: {
    // 高さ 520 のまま 90px 上へ（2026-08-23）。章タグの帯との間隔は
    // 130 → 64px に詰まるが、フォントサイズは高さから逆算されるので
    // 見出しの見た目は変わらない。
    headline: { top: 960 / 1920, height: 520 / 1920 },
    subtitle: { top: 1480 / 1920, height: 440 / 1920 },
  },
} as const;

/** 動画の形式。Python 側の `VideoFormat`（`src/models/formats.py`）と同じ集合。 */
export type VideoFormat = "short" | "tiktok" | "long";

/**
 * プラットフォームの UI に明け渡すフレーム下端の帯（フレーム高に対する比率）。
 *
 * YouTube Shorts の再生画面は下端の約 150px を UI（チャンネルアイコン・
 * チャンネル名・タイトル・シークバー）が覆う。**そこに文字を置くと、尺の
 * 全体にわたって字幕の最終行が読めない**（実測: UI の上端は y=1768。
 * 直す前は文字の下端が 1848px にあり、行数に関わらず最終行が丸ごと
 * 隠れていた。1行の字幕はその1行が読めなかった）。
 *
 * **オーナーにだけ出る「このショート動画を宣伝する」ボタン（下端から
 * 205px）を基準にしない。** 一般の視聴者には出ないので、守るべき最小値は
 * 150px の方。宣伝ボタンはオーナーが自分の動画を確認するときだけ余分に覆う。
 *
 * **Google 公式のセーフエリアは採らない。** 公式のオーバーレイ画像
 * （`youtubesafezoneoverlay_vertical_final.png`、1080x1920）を実測すると
 * 安全域は x 48..887 / y 288..1247——下 672px・右 192px・上 288px を空けろ
 * という値だった。あれは*広告*在庫のセーフゾーンで、CTA バナーと見出しを
 * 重ねるぶんを含む。下 672px を空けると使えるフレームは 960px しか残らず、
 * 挿絵・見出し・字幕の構成が成立しない。**一次情報だが面が違う。**
 * （右 192px は Shorts の右側ボタン列とほぼ一致するので、そちらを実測する
 * ときの手掛かりとして記録しておく。字幕は左寄せなので、いま当たりうるのは
 * 右端まで届く長い行だけ。）
 *
 * 比率で持つ理由はゾーンと同じ（`long` は 1920x1080 で高さが 1080 になる）。
 */
const SAFE_BOTTOM: Record<VideoFormat, number> = {
  // オーナーの実測（Shorts の再生画面のスクリーンショット、1080x1920 換算）。
  short: 150 / 1920,
  // **未実測。short から借りている値。** TikTok の UI は Shorts より広く
  // 覆うはずなので、実測したらここだけを差し替える。借り物であることを
  // 明示しておくのは、測った値の置き場所を自明にするため。
  tiktok: 150 / 1920,
  // 横画面。通常動画の UI は常時表示ではないので空けない。
  long: 0,
};

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

/**
 * その形式で下端に空けるべき高さをピクセルで返す。
 *
 * **未知の形式名で NaN にしない。** props は JSON 経由で来るので、TS の型が
 * 通っていても実行時に表の外の文字列が入りうる。`undefined` を返すと
 * 呼び出し側の算術が NaN になり、パディングが**静かに**壊れる（例外にならず、
 * 画面を見るまで気付けない）。Python 側の `get_spec()` が未知の形式名を
 * `short` に落とすのと同じ挙動に揃える。
 */
export function useSafeBottom(format: VideoFormat): number {
  const { height } = useVideoConfig();
  return (SAFE_BOTTOM[format] ?? SAFE_BOTTOM.short) * height;
}
