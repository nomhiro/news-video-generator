import { Composition } from "remotion";
import { NewsVideo, SAMPLE_PROPS } from "./Video";

/**
 * コンポジションは1つだけ。
 *
 * 解像度・fps・尺は props から `calculateMetadata` で受ける。Composition に
 * 固定値を書くと形式（short / tiktok / long）ごとに定義が増え、
 * `src/models/formats.py` が単一の情報源であることが崩れる。
 */
export const RemotionRoot: React.FC = () => (
  <Composition
    id="NewsVideo"
    component={NewsVideo}
    // calculateMetadata が上書きするが、Composition は初期値を要求する
    durationInFrames={SAMPLE_PROPS.durationInFrames}
    fps={SAMPLE_PROPS.fps}
    width={SAMPLE_PROPS.width}
    height={SAMPLE_PROPS.height}
    defaultProps={SAMPLE_PROPS}
    calculateMetadata={({ props }) => ({
      durationInFrames: props.durationInFrames,
      fps: props.fps,
      width: props.width,
      height: props.height,
    })}
  />
);
