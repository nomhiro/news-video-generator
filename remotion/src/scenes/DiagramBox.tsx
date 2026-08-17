import { useReveal } from "../beats";
import { fitFontSize } from "../fitText";
import { RoughRect } from "../Rough";
import { COLORS, FONT_STACK } from "../theme";

/** ラベルのプレートの左右パディング。 */
const PLATE_PADDING_X = 18;

/** 箱の内側に残す余白。ラベルが枠線にぶつからないための最低限。 */
const BOX_INSET = 24;

/**
 * 箱幅からラベルの文字に使える幅を出すために引く量。
 *
 * 呼び出し側（`Compare`）が2つの箱で共通のサイズを求めるのに必要なので
 * 公開している。ここと計算を二重に持つと、片方だけ変えたときに
 * ラベルが箱からはみ出す。
 */
export const LABEL_INSET = BOX_INSET * 2 + PLATE_PADDING_X * 2;

/** ラベルの既定の上限サイズ。 */
export const DEFAULT_LABEL_SIZE = 84;

/**
 * ラベルの高さの上限（箱の高さに対する比）。
 *
 * 幅だけで決めると、`compare` の縦長の箱で2文字のラベルが箱を埋め尽くす
 * ほど大きくなる。名札として成立する範囲に抑える。
 */
const MAX_HEIGHT_RATIO = 0.42;

/**
 * `compare` / `flow` で使う手描き風の箱。
 *
 * ラベルはハッチング塗りの上に直接置かない。斜線の塗りが `入力` /
 * `専門家選択` のような文字を貫くと読みにくくなる（実測で確認した不具合）。
 * 地の色に近いプレートを文字の背後に敷き、そこだけ塗りを避ける。
 *
 * **ラベルのサイズは文字数と箱の幅から逆算する**（`fontSize` は上限）。
 * 固定サイズだったときは、`items` に許された8文字のうち5文字
 * （`従来モデル`）で既に折り返し、`従来モデ` / `ル` と語の途中で割れたうえ、
 * 折り返した行がプレートを箱幅いっぱいに広げて「名札」に見えなくなっていた
 * （実測）。1行に収める前提で縮めれば、8文字でも割れない。
 */
export const DiagramBox: React.FC<{
  text: string;
  width: number;
  height: number;
  color: string;
  seed: number;
  reveal: ReturnType<typeof useReveal>;
  /** ラベルの上限サイズ。実際のサイズは文字数と幅から下に丸められる。 */
  fontSize?: number;
}> = ({ text, width, height, color, seed, reveal, fontSize = DEFAULT_LABEL_SIZE }) => {
  const available = width - LABEL_INSET;
  const labelSize = Math.min(
    fitFontSize(text, available, fontSize),
    height * MAX_HEIGHT_RATIO,
  );
  return (
  <div style={{ position: "relative", width, height, opacity: reveal.opacity }}>
    <RoughRect
      width={width}
      height={height}
      seed={seed}
      stroke={color}
      fill={color}
      drawProgress={reveal.drawProgress}
      fillOpacity={reveal.fillProgress * 0.55}
    />
    <div
      style={{
        position: "relative",
        height: "100%",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <span
        style={{
          fontFamily: FONT_STACK,
          fontSize: labelSize,
          fontWeight: 800,
          color: COLORS.text,
          backgroundColor: COLORS.plate,
          padding: `6px ${PLATE_PADDING_X}px`,
          borderRadius: 10,
          // 1行に収める前提でサイズを決めているので、折り返しは禁止する。
          // 折り返るとプレートが箱幅いっぱいに広がり、名札ではなく
          // 「箱を横切る帯」に見える（実測）。
          whiteSpace: "nowrap",
          textAlign: "center",
        }}
      >
        {text}
      </span>
    </div>
  </div>
  );
};
