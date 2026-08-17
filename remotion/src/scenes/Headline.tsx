import { spring, useCurrentFrame, useVideoConfig } from "remotion";
import { COLORS, FONT_STACK } from "../theme";
import type { Zone } from "../zones";

// Python 側（`src/utils/line_break.py`）が挿入する、日本語のフレーズ境界の
// ゼロ幅スペース。表示上は幅を持たないので、縮小率を決める文字数の計算からは
// 除外する必要がある（除外しないと、ZWSP の分だけ「文字数」が水増しされ、
// 見出しが必要以上に縮む——改行は直っても文字が小さくなる、という分かりにくい
// 退行になる）。
const ZWSP = "​";

/**
 * 1行に収まる文字数の見積り（全角、フォントサイズが `MEASURED_AT_SIZE` px のとき）。
 *
 * 92px・幅952px の理論上の描画幅からは約10字/行と見積もれるが、これは
 * **実測と食い違っていた**。BudouX が挿入したフレーズ境界（ZWSP）で改行する
 * 実装では、行はフレーズの区切りでしか折れないため、理論上の文字数まで
 * 詰めることができず、常に手前で折れる。実際にレンダリングしたフレームで
 * 確かめたところ、18字の見出し（`推論コストが一桁下がる新しい学習方式`）は
 * 92px のまま3行に収まっているにもかかわらず、その改行は
 * 「推論コストが / 一桁下がる新しい / 学習方式」（6/8/4字）で、
 * どの行も8字を超えていなかった。フレーズ境界改行がある限り理論値は
 * 使えないので、実測の8を使う（Latin は advance が半分程度なので、この
 * 見積りは英語では**過小**になる。安全側なので単純さを取っている）。
 */
const CHARS_PER_LINE_AT_BASE = 8;

/** 上の実測値を取ったときのフォントサイズ。文字数/行は概ねこれに反比例する。 */
const MEASURED_AT_SIZE = 92;

/** 行の高さ（フォントサイズに対する比）。 */
const LINE_HEIGHT = 1.28;

/** 縮める下限（`size` に対する比）。これ以上小さくすると見出しの役を果たさない。 */
const MIN_SCALE = 0.5;

/**
 * 見出し。`Script.text_overlays[i]` が入る。
 *
 * **フォントサイズはゾーンの高さから逆算する。** 以前は
 * `MAX_LINES = 3` を全レイアウト共通の固定値として使っていたが、
 * `statement`（図が無く見出しがほぼ全体を占める）と `compare`/`flow`
 * （見出しは図の上の狭い帯）とでは使える縦の空間が全く違う。
 * 固定の行数制限は「一番狭いレイアウトで安全な値」を全部に強制することになり、
 * 逆に一番広いレイアウトでは早すぎる文字欠落を起こしていた（実測で
 * `statement`・`size=112`・45字の見出しが欠けたのがこれ）。
 *
 * 導出:
 *   文字数/行 ≈ CHARS_PER_LINE_AT_BASE × (MEASURED_AT_SIZE / fontSize)
 *   使える行数 = zone.height / (fontSize × LINE_HEIGHT)
 *   収まる文字数 = 文字数/行 × 使える行数
 *              = (CHARS_PER_LINE_AT_BASE × MEASURED_AT_SIZE × zone.height)
 *                / (LINE_HEIGHT × fontSize^2)
 * これを `収まる文字数 >= visibleLength` で解くと、
 *   fontSize <= sqrt(CHARS_PER_LINE_AT_BASE × MEASURED_AT_SIZE × zone.height
 *                     / (LINE_HEIGHT × visibleLength))
 * になる（比例のみの近似なので `ceil` による行数の離散段差は無視している。
 * 元の実装も同じ精度で「予算」を扱っていたので、精度は後退していない）。
 */
export const Headline: React.FC<{
  text: string;
  zone: Zone;
  size?: number;
  startFrame?: number;
}> = ({
  text,
  size = 92,
  // ビート演出（chapter → headline → ...）に合わせて出現を遅らせるための
  // オフセット。既定の0は「シーン開始と同時に出る」旧来の挙動と一致する。
  startFrame = 0,
  zone,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const enter = spring({ frame: frame - startFrame, fps, config: { damping: 200 } });

  // 長い見出しは字数とゾーンの高さに応じて縮める。
  //
  // 一次の防波堤は `script.MAX_HEADLINE_CHARS` のバリデータで、ここは保険。
  // ゾーンの外まで伸びた見出しは下の要素（図・字幕）と重なるか画面外に切れる。
  // **壊れた文字列を「小さい文字」で済ませる**のがここの役割で、
  // レイアウトの衝突より読みにくさの方が実害が小さい。
  // ZWSP は表示幅0なので、縮小率の計算対象の文字数から除く（上の定数コメント参照）。
  const visibleLength = Math.max(text.split(ZWSP).join("").length, 1);
  const idealFontSize = Math.sqrt(
    (CHARS_PER_LINE_AT_BASE * MEASURED_AT_SIZE * zone.height) / (LINE_HEIGHT * visibleLength),
  );
  const fontSize = Math.max(size * MIN_SCALE, Math.min(size, idealFontSize));

  return (
    <div
      style={{
        position: "absolute",
        top: zone.top,
        height: zone.height,
        left: 0,
        right: 0,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "0 72px",
      }}
    >
      <h1
        style={{
          fontFamily: FONT_STACK,
          fontSize,
          fontWeight: 900,
          color: COLORS.text,
          textAlign: "center",
          lineHeight: LINE_HEIGHT,
          margin: 0,
          // 縮めても収まらない病的な入力に対する最後の歯止め。ゾーンの外に
          // 出さない——ゾーンの外は別の要素の領域という契約そのものを守る。
          maxHeight: zone.height,
          overflow: "hidden",
          // `word-break: auto-phrase` を試したが実測で無効だった（Subtitle.tsx
          // 参照）。BudouX が挿入した ZWSP を良い改行点として使い、`keep-all` で
          // CJK 文字間の悪い改行点を禁止する。
          wordBreak: "keep-all",
          // ZWSP を挟んでも1フレーズが1行に収まらない病的な入力への安全弁。
          overflowWrap: "anywhere",
          transform: `translateY(${(1 - enter) * 40}px)`,
          opacity: enter,
        }}
      >
        {text}
      </h1>
    </div>
  );
};
