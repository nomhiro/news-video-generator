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
    // 画面最下部から約300px持ち上げる。以前は真下に張り付いていて低すぎた。
    // スクリムの帯自体を持ち上げるので、その下の帯には何も敷かれない
    // （地がほぼフラットな色のため、そこだけ見えても違和感は出ない）。
    <AbsoluteFill style={{ justifyContent: "flex-end", opacity, paddingBottom: 300 }}>
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
            // `word-break: auto-phrase` を試したが実測で無効だった
            // （実際のフレームで「絞ったこ」/「とでした。」のように
            // 「ことでした」が割れた）。ブラウザのフレーズ推定に頼らず、
            // Python 側で BudouX がフレーズ境界に挿入した ZWSP
            // （`insert_break_opportunities`）を「良い改行点」として使い、
            // `keep-all` で CJK 文字間の「悪い改行点」を禁止する。
            // 両方揃わないと機能しない（keep-all 単体は折れずに溢れ、
            // ZWSP 単体は悪い位置でも折れてしまう）。
            wordBreak: "keep-all",
            // ZWSP を挟んでもフレーズ自体が1行に収まらない病的な入力への
            // 安全弁。通常は ZWSP の位置でしか折れないが、収まらない場合は
            // 任意の位置で折って画面外への溢れを防ぐ。
            overflowWrap: "anywhere",
          }}
        >
          {text}
        </span>
      </div>
    </AbsoluteFill>
  );
};
