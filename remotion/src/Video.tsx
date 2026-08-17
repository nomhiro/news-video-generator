import { AbsoluteFill, Sequence } from "remotion";
import { Background } from "./Background";
import { Subtitle } from "./Subtitle";
import { Compare } from "./scenes/Compare";
import { Flow } from "./scenes/Flow";
import { Statement } from "./scenes/Statement";
import { COLORS } from "./theme";

export type SceneProps = {
  layout: "statement" | "compare" | "flow";
  items: string[];
  /** 見出し。Script.text_overlays[i] */
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
  durationInFrames: number;
  scenes: SceneProps[];
};

const LAYOUTS = {
  statement: Statement,
  compare: Compare,
  flow: Flow,
} as const;

export const NewsVideo: React.FC<VideoProps> = ({ scenes }) => (
  <AbsoluteFill style={{ backgroundColor: COLORS.bg }}>
    <Background />
    {scenes.map((scene, i) => {
      const Layout = LAYOUTS[scene.layout];
      return (
        <Sequence
          key={i}
          from={scene.fromFrame}
          durationInFrames={scene.durationInFrames}
        >
          <Layout headline={scene.headline} items={scene.items} />
          <Subtitle text={scene.subtitle} />
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
  durationInFrames: 90,
  scenes: [
    {
      layout: "statement",
      items: [],
      headline: "推論コストが桁で下がる",
      subtitle: "推論のコストが一桁下がる、という話です。",
      fromFrame: 0,
      durationInFrames: 30,
    },
    {
      layout: "compare",
      items: ["従来", "新方式"],
      headline: "何が変わったのか",
      subtitle: "変わったのは、動かす範囲を絞ったことでした。",
      fromFrame: 30,
      durationInFrames: 30,
    },
    {
      layout: "flow",
      items: ["入力", "選択"],
      headline: "仕組み",
      subtitle: "入力ごとに、使う専門家を切り替えています。",
      fromFrame: 60,
      durationInFrames: 30,
    },
  ],
};
