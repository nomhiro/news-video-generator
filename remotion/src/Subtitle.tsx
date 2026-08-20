import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { COLORS, FONT_STACK } from "./theme";
import type { Zone } from "./zones";

/**
 * 画面下の字幕。ナレーションのセグメントをそのまま出す。
 *
 * 黄色文字＋不透明な黒ボックス（drawtext 時代のスタイル）はやめ、
 * 下端のスクリム（グラデーション）に白文字を置く。ボックスの輪郭が
 * 出ないので、量産系まとめ動画の記号にならない。
 *
 * **位置は `zone`（`zones.ts`）から取る。** 以前は「画面最下部から
 * 300px 持ち上げる」という固定値だったが、compare/flow では図がどこまで
 * 伸びるかに応じて字幕の開始位置も変わる必要がある（実測で図の下端と
 * 字幕が重なる不具合が見つかった）。ゾーンを1箇所で管理することで、
 * 図と字幕が同じ座標系を参照し、重なりが構造的に起きなくなる。
 */
export const Subtitle: React.FC<{ text: string; zone: Zone }> = ({ text, zone }) => {
  const frame = useCurrentFrame();
  const opacity = interpolate(frame, [0, 6], [0, 1], {
    extrapolateRight: "clamp",
  });
  return (
    // top・bottom の両方を明示し、`height` は既定の "100%" を打ち消して
    // "auto" にする。`AbsoluteFill` は既定で height:100% も持っており、
    // top/bottom/height を同時に指定すると CSS は height を優先して
    // bottom を無視する（画面外まで伸びて中身が下に消える不具合が実際に
    // 起きた）。height を明示的に外すことで top〜bottom の範囲に収める。
    <AbsoluteFill
      style={{
        top: zone.top,
        bottom: 0,
        height: "auto",
        justifyContent: "flex-end",
        // 字幕は下端寄せなので、**長い字幕は上に伸びる**。ゾーンの上端を
        // 越えた先は図の領域で、そこに食い込めば直したはずの重なりが
        // 再発する。ゾーンの外には描かせないことで構造的に防ぐ
        // （読みにくさより重なりの方が実害が大きい、という判断は
        // `Headline` の `maxHeight` と同じ）。
        overflow: "hidden",
        opacity,
      }}
    >
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
