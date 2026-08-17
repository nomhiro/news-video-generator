import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { COLORS, FONT_STACK } from "./theme";

/**
 * 画面下の字幕。ナレーションのセグメントをそのまま出す。
 *
 * 黄色文字＋不透明な黒ボックス（drawtext 時代のスタイル）はやめ、
 * 下端のスクリム（グラデーション）に白文字を置く。ボックスの輪郭が
 * 出ないので、量産系まとめ動画の記号にならない。
 */
export const Subtitle: React.FC<{ text: string }> = ({ text }) => {
  const frame = useCurrentFrame();
  const opacity = interpolate(frame, [0, 6], [0, 1], {
    extrapolateRight: "clamp",
  });
  return (
    <AbsoluteFill style={{ justifyContent: "flex-end", opacity }}>
      <div
        style={{
          padding: "160px 72px 96px",
          background:
            "linear-gradient(to top, rgba(0,0,0,0.78) 0%, rgba(0,0,0,0.55) 55%, rgba(0,0,0,0) 100%)",
        }}
      >
        <span
          style={{
            fontFamily: FONT_STACK,
            fontSize: 54,
            fontWeight: 700,
            color: COLORS.text,
            lineHeight: 1.45,
            // 日本語を単語単位で折る。機械的に N 文字で切ると
            // 「推論コストが桁で下 / がる」のように不自然な位置で折れる。
            wordBreak: "auto-phrase",
          }}
        >
          {text}
        </span>
      </div>
    </AbsoluteFill>
  );
};
