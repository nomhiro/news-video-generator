import { AbsoluteFill, Sequence } from "remotion";
import { Background } from "./Background";
import { Illustration } from "./Illustration";
import { Compare } from "./scenes/Compare";
import { Flow } from "./scenes/Flow";
import { Statement } from "./scenes/Statement";
import { COLORS } from "./theme";
import type { VideoFormat } from "./zones";

/**
 * 各レイアウトが受け取る props。`layout` によって使わないフィールドが
 * あるが（`statement` は `items` / `relation` を使わない等）、3つの
 * レイアウトを同じ型で扱えるようにあえて統一している
 * （`LAYOUTS[scene.layout]` を単一の型で呼べる）。
 *
 * `Subtitle` は各レイアウトが自分のゾーン（`zones.ts`）を使って自分で描く。
 * 以前は `Video.tsx` が `Layout` と `Subtitle` を並べて置いていたが、それだと
 * 字幕の位置が図のゾーンと無関係に決まり、図が伸びると重なった
 * （実測で確認した不具合）。同じゾーン定義を参照させるため、描画も
 * レイアウト側に持たせる。
 */
export type LayoutProps = {
  /** 見出し。Script.text_overlays[i] */
  headline: string;
  /** 字幕。Script.segment_narrations[i] */
  subtitle: string;
  /** compare/flow は2要素、statement は空配列。 */
  items: string[];
  /** compare/flow の2要素の関係を表す短い語（例: "1/10"）。statement は "" */
  relation: string;
  /** 章ラベル（例: "仕組み"）。空文字列なら ChapterTag は何も描かない。 */
  chapter: string;
  /**
   * 動画の形式。字幕がプラットフォームの UI をどれだけ避けるかを決める
   * （`zones.ts` の `SAFE_BOTTOM`）。解像度だけでは決められない——
   * `short` と `tiktok` は同じ 1080x1920 だが、覆われる高さは同じとは
   * 限らない（いまは `tiktok` が `short` から値を借りている）。
   */
  format: VideoFormat;
  durationInFrames: number;
};

export type SceneProps = {
  layout: "statement" | "compare" | "flow";
  items: string[];
  relation: string;
  chapter: string;
  headline: string;
  /** 字幕。Script.segment_narrations[i] */
  subtitle: string;
  /**
   * フレーム範囲は **Python 側で解決済み**のものを受ける。
   * 単調増加の強制と「タイミングの要素数はセグメント数+1」という契約は
   * 既に Python にあるため、同じ計算をここに持たせない。
   */
  fromFrame: number;
  durationInFrames: number;
};

export type VideoProps = {
  width: number;
  height: number;
  fps: number;
  /** 形式名。Python 側が `src/models/formats.py` の名前をそのまま渡す。 */
  format: VideoFormat;
  durationInFrames: number;
  /**
   * 動画全体で共有する挿絵のファイル名（`remotion/public/` からの相対名）。
   * 空文字列なら地のみで描く（挿絵の生成に失敗した場合もここに空文字列が
   * 渡る。装飾的な要素の欠落で動画本体を失敗させない、という Python 側の
   * 判断をそのまま受ける——ここで代替の図形を描いて「壊れて見える」ように
   * しない）。
   */
  illustration: string;
  scenes: SceneProps[];
};

const LAYOUTS = {
  statement: Statement,
  compare: Compare,
  flow: Flow,
} as const;

export const NewsVideo: React.FC<VideoProps> = ({ illustration, scenes, format }) => (
  <AbsoluteFill style={{ backgroundColor: COLORS.bg }}>
    <Background />
    {/* すべての `<Sequence>` の外（トップレベル）で描く。`Illustration.tsx`
        のコメント参照——ここに置くことで、絶対フレームを基準にした
        ドリフトがシーン切替をまたいで続く（シーンの内側に置くと
        `useCurrentFrame()` がシーケンス相対になり、切替ごとにリセットされる）。 */}
    <Illustration filename={illustration} />
    {scenes.map((scene, i) => {
      const Layout = LAYOUTS[scene.layout];
      return (
        <Sequence
          key={i}
          from={scene.fromFrame}
          durationInFrames={scene.durationInFrames}
        >
          <Layout
            headline={scene.headline}
            subtitle={scene.subtitle}
            items={scene.items}
            relation={scene.relation}
            chapter={scene.chapter}
            format={format}
            durationInFrames={scene.durationInFrames}
          />
        </Sequence>
      );
    })}
  </AbsoluteFill>
);

/**
 * Studio を開いたときと、props を渡さずにレンダリングしたときの既定値。
 * 実運用では Python が --props でファイル経由の JSON を渡す。
 */
export const SAMPLE_PROPS: VideoProps = {
  width: 1080,
  height: 1920,
  fps: 30,
  format: "short",
  durationInFrames: 90,
  // Studio 上での見た目確認用。実運用では Pipeline が `remotion/public/` に
  // 置いたファイル名を渡す（`RemotionRenderer._place_illustration`）。
  illustration: "",
  scenes: [
    {
      layout: "statement",
      items: [],
      relation: "",
      chapter: "",
      headline: "推論コストが桁で下がる",
      subtitle: "推論のコストが一桁下がる、という話です。",
      fromFrame: 0,
      durationInFrames: 30,
    },
    {
      layout: "compare",
      items: ["従来", "新方式"],
      relation: "1/10",
      chapter: "事実",
      headline: "何が変わったのか",
      subtitle: "変わったのは、動かす範囲を絞ったことでした。",
      fromFrame: 30,
      durationInFrames: 30,
    },
    {
      layout: "flow",
      items: ["入力", "専門家選択"],
      relation: "切替",
      chapter: "仕組み",
      headline: "仕組み",
      subtitle: "入力ごとに、使う専門家を切り替えています。",
      fromFrame: 60,
      durationInFrames: 30,
    },
  ],
};
