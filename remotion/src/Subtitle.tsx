import { AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig } from "remotion";
import { estimateEmWidth } from "./fitText";
import { COLORS, FONT_STACK } from "./theme";
import { useSafeBottom, type VideoFormat, type Zone } from "./zones";

/**
 * 字幕の基準フォントサイズ。
 *
 * **54 → 46（2026-08-22）。54px では4行の字幕がゾーンに入らなかった。**
 * 実測（1080x1920 / 46.2秒のショート1本）で、6シーンのうち3シーンの字幕が
 * 1行目を横一直線に断ち切られており、**尺の約52%（24秒）で文字が読めない**
 * 状態だった。原因は下の `SCRIM_*` のコメントに書いたパディングの食い潰しと、
 * 「4行ぶんの高さがゾーンに無い」ことの二重。
 *
 * 46 の根拠は逆算である。ゾーン440px からパディング（12 + 162）を引いた
 * 266px に、行送り `LINE_HEIGHT`（1.38）で**4行**が収まる最大値が
 * 266 / (4 × 1.38) = 48.2px。48 では余裕が1pxしか無く、行送りの丸めや
 * フォントの差（ローカル Yu Gothic / 本番 Noto Sans CJK）を吸収できないので
 * 46 を採る（4行 = 254px、余裕12px）。
 *
 * **266px は 2026-08-23 の変更でも変えていない。** プラットフォームの UI を
 * 避けるために下パディングを 72→162 に増やしたぶん、ゾーンを 350→440 に
 * 伸ばして相殺した（`zones.ts`）。この値が変わるとフォントサイズの根拠が
 * 崩れるので、片方だけ動かさないこと。
 *
 * **1〜4行では常にこのサイズで描く。** シーンごとに字幕の大きさが変わると
 * それ自体が粗く見えるので、縮小は「4行に収まらない病的な入力」に限る
 * （`fitSubtitleSize`）。
 */
const BASE_SIZE = 46;

/**
 * 行送り（フォントサイズに対する比）。
 *
 * 1.45 → 1.38。4行をゾーンに収めるために詰めた。日本語の字幕としては
 * まだ緩い方で、ffmpeg レンダラの drawtext は 76/64 = 1.19 で運用していた。
 */
const LINE_HEIGHT = 1.38;

/**
 * ゾーン上端に残す余白。
 *
 * 0 にすると最上行のアセンダがゾーン境界に接し、`overflow: hidden` の
 * 丸め誤差1pxで文字の頭が削れる。**「切れていない」を画素で判定する検査
 * （`tests/test_remotion_render_slow.py`）が境界行の明画素を見ている**ので、
 * 意図せず接することそのものを避ける。
 */
const PADDING_TOP = 12;

/**
 * フレーム下端に残す余白の**下限**。
 *
 * 96 → 72（4行ぶんの高さを作るために削った）。**2026-08-23 から下限として
 * だけ使う。** 実際の余白はプラットフォームの UI が覆う帯
 * （`zones.ts` の `SAFE_BOTTOM`）から導く——「UI が重なるので 0 にはしない」
 * とだけ書いて 72 を置いていたが、**実測 150px の半分未満**で、字幕の最終行は
 * 行数に関わらず丸ごと UI の下に隠れていた（Issue #44）。
 *
 * 下限として残す理由は `long`（横画面、UI の帯を空けない形式）。そこでは
 * 導出値が 12px になり、文字がフレーム下端に寄ってしまう。
 */
const MIN_PADDING_BOTTOM = 72;

/**
 * セーフライン（UI の帯の上端）と文字の間に空ける余白。
 *
 * 0 にすると行ボックスの下端がセーフラインに接する。実測値そのものに文字を
 * 寄せて得るものは無いので、丸めやフォントの差（ローカル Yu Gothic / 本番
 * Noto Sans CJK）を吸収するぶんだけ空ける（`PADDING_TOP` と同じ趣旨）。
 */
const BOTTOM_CLEARANCE = 12;

/** 左右の余白。フレーム幅から引いて1行に使える幅を出す。 */
const PADDING_X = 72;

/**
 * 1行に詰められる幅の、理論値に対する比。
 *
 * BudouX が挿入した ZWSP の位置でしか折り返せないため、行末には必ず
 * 「次のフレーズが入り切らない」端数が残り、理論上の幅いっぱいまでは詰まらない。
 * 実測フレーム（`fontSize=54`、使える幅 936px = 17.33em）での行の占有:
 *
 * - `サイレントAIユーザーにも新しい` → 15.1em（87%）
 * - `Claudeの文章に、目に見えない` → 14.3em（82%）
 * - `添えるだけで済む話が、` → 11.0em（63%、文の切れ目なので短い）
 *
 * 行数の見積りに使うのは「最も詰まった行」ではなく「平均的にどれだけ詰まるか」
 * なので、上2つの実測（82〜87%）の下側を採って 0.82 とする。
 * **過小に見積もると行数を多く数えて不必要に縮み、過大に見積もると
 * 収まらない**。前者は読みにくくなるだけ、後者は文字が切れるので、
 * 迷ったら過小側（小さい値）に寄せる。
 */
const LINE_PACKING = 0.82;

/** 縮小の下限。これ以上小さいと字幕として読めない。 */
const MIN_SIZE = 30;

/**
 * ゾーンに収まる最大のフォントサイズを返す。
 *
 * `BASE_SIZE` から1pxずつ下げ、見積り行数 × 行送りが使える高さに収まる
 * 最初の値を採る。**連続式を解いて `ceil` の段差を無視する近似は使わない**
 * ——`Headline` はその近似の段差を安全係数 0.85 で吸収しており、その係数は
 * 実測に対して2度外れている（`Headline.tsx` のコメント参照）。ここは
 * 候補が17個しか無いので、段差込みで素直に走査する方が正確で読みやすい。
 */
export function fitSubtitleSize(text: string, availableWidth: number, availableHeight: number) {
  const em = estimateEmWidth(text);
  for (let size = BASE_SIZE; size > MIN_SIZE; size -= 1) {
    const emPerLine = (availableWidth / size) * LINE_PACKING;
    const lines = Math.max(1, Math.ceil(em / emPerLine));
    if (lines * size * LINE_HEIGHT <= availableHeight) return size;
  }
  return MIN_SIZE;
}

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
 *
 * **スクリムはテキストの箱に持たせない（2026-08-22）。** 以前は
 * `padding: "160px 72px 96px"` を当てた div にグラデーションを敷いていた。
 * グラデーションを滑らかに立ち上げるための上パディング160pxが**ゾーンの
 * 高さ（当時 350px、いまは 440px）を食い**、テキストに使える高さは
 * 350 − 160 − 96 = **94px**
 * ——1.2行ぶんしか残っていなかった。3行（235px）ですでにゾーン上端を
 * 越えており、`overflow: hidden` が上を切っていた（3行は境界の上が
 * パディングだったので偶然無事、4行（313px）で1行目が切れた）。
 * **スクリムはゾーン全体を覆う独立した層**にすれば、パディングは
 * 「文字を端から離す」ぶんだけで済む。副作用として、切られた
 * グラデーションが作っていたゾーン上端（当時 y=1570）の硬い横線も消える。
 */
export const Subtitle: React.FC<{ text: string; zone: Zone; format: VideoFormat }> = ({
  text,
  zone,
  format,
}) => {
  const frame = useCurrentFrame();
  const { width } = useVideoConfig();
  const safeBottom = useSafeBottom(format);
  const opacity = interpolate(frame, [0, 6], [0, 1], {
    extrapolateRight: "clamp",
  });
  const availableWidth = width - PADDING_X * 2;
  // **ゾーンは下端まで伸ばしたまま、文字だけを UI の上へ持ち上げる。**
  // ゾーンを縮めて逃げると、下端のスクリムはゾーンの高さと無関係に必ず
  // フレーム下端まで伸びるため、ゾーンの値がレイアウトの実態を表さなくなり
  // 「ゾーンで重なりを防ぐ」仕組み全体が信じられなくなる（`zones.ts`）。
  const paddingBottom = Math.max(MIN_PADDING_BOTTOM, safeBottom + BOTTOM_CLEARANCE);
  const availableHeight = zone.height - PADDING_TOP - paddingBottom;
  const fontSize = fitSubtitleSize(text, availableWidth, availableHeight);

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
        // `Headline` の `maxHeight` と同じ）。`fitSubtitleSize` が
        // 収まるサイズを選んだ上での、最後の歯止め。
        overflow: "hidden",
        opacity,
      }}
    >
      {/* スクリム。ゾーン全体を覆う独立した層にする（上のコメント参照）。
          文字の箱に持たせるとパディングがテキスト領域を食う。 */}
      <AbsoluteFill
        style={{
          background:
            "linear-gradient(to top, rgba(0,0,0,0.80) 0%, rgba(0,0,0,0.58) 55%, rgba(0,0,0,0) 100%)",
        }}
      />
      <div
        style={{
          position: "relative",
          padding: `${PADDING_TOP}px ${PADDING_X}px ${paddingBottom}px`,
        }}
      >
        <span
          style={{
            fontFamily: FONT_STACK,
            fontSize,
            fontWeight: 700,
            color: COLORS.text,
            lineHeight: LINE_HEIGHT,
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
            // 影を足す理由: スクリムを1層に分けたぶん、地の明るい挿絵が
            // 近い位置に来ても文字が沈まないようにする（挿絵は白地）。
            textShadow: "0 2px 12px rgba(0,0,0,0.55)",
            display: "block",
          }}
        >
          {text}
        </span>
      </div>
    </AbsoluteFill>
  );
};
