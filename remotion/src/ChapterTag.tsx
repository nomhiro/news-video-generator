import { useReveal } from "./beats";
import { COLORS, FONT_STACK } from "./theme";
import type { Zone } from "./zones";

/** 挿絵の左端から空ける余白。 */
const LEFT_MARGIN = 48;

/**
 * 挿絵の下端寄りに重ねる小さなタグ。`chapter`（例: "仕組み"）を表示する。
 *
 * 「いま構成のどの段（フック/事実/仕組み/インパクト/結論）にいるか」を
 * 視聴者に示す。挿絵の上に置く唯一の文字要素なので、**背景は不透明**にする
 * ——挿絵は記事ごとに絵柄も明度もばらばらで、透明や半透明の背景では、
 * 明るい挿絵の上で文字が読めなくなる場面が起こりうる。地の色そのもの
 * （`COLORS.bg`）を不透明な板として敷き、フラットな枠線だけを重ねる
 * （手描き風の揺れる枠線はやめた——手描きの表現全体を退役させた
 * restyle の一部）。
 *
 * 以前は画面上部の空き（中央寄せ）を埋める要素だったが、その空きは挿絵に
 * 明け渡した。位置は**挿絵の下端寄りの左**に変わり、水平中央寄せは廃止した
 * （挿絵が主役なので、タグは隅に控える方が挿絵を邪魔しない）。ゾーン自体の
 * 位置は `zones.ts` の `shared.chapter` が単一の情報源——実写で章タグが
 * 円形アイコンに重なって隠した反省から、挿絵の下端フェードが最も深く効く
 * 位置まで既に沈めてある。
 *
 * `chapter` が空文字列のときは**何も描かない**。Python 側が事情により
 * ラベルを付けられない場合に空文字列で「劣化」させて渡す契約になっている
 * （失敗させるのではなく、タグ無しで動画は成立させる）。
 */
export const ChapterTag: React.FC<{
  text: string;
  startFrame: number;
  /** 章タグのゾーン（`zones.ts` の `shared.chapter`）。ゾーンの中央に縦寄せする。 */
  zone: Zone;
}> = ({ text, startFrame, zone }) => {
  const { opacity } = useReveal(startFrame);
  if (!text) return null;

  const width = 64 + text.length * 34;
  const height = 76;

  return (
    <div
      style={{
        position: "absolute",
        // ゾーンの中央に縦寄せ。ゾーンの外に出ないので、挿絵の下端を
        // 越えて字幕側へ食い込むことは構造的に起こらない。
        top: zone.top + (zone.height - height) / 2,
        left: LEFT_MARGIN,
        width,
        height,
        opacity,
        backgroundColor: COLORS.bg,
        border: `2px solid ${COLORS.accent}`,
        borderRadius: 10,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <span
        style={{
          fontFamily: FONT_STACK,
          fontSize: 34,
          fontWeight: 700,
          color: COLORS.text,
          padding: "4px 14px",
        }}
      >
        {text}
      </span>
    </div>
  );
};
