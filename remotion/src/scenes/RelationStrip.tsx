import type { Reveal } from "../beats";
import { fitFontSize } from "../fitText";
import { COLORS, FONT_STACK } from "../theme";
import type { Zone } from "../zones";

/** 項目ラベルの上限サイズ。1行の帯なので `DiagramBox` の名札より小さい。 */
const ITEM_MAX_SIZE = 44;
/**
 * 関係ラベルの上限サイズ。
 *
 * **38 → 32（2026-08-22）。プレートがコネクタの線を完全に覆っていた。**
 * 旧実装はプレートを `top: -relationSize * 1.05` で持ち上げていたが、
 * span の行ボックスは `fontSize * 1.4`（既定の line-height）＋パディングで、
 * **持ち上げ量よりつねに背が高い**。実測（`relation="義務化"`、37.3px）では
 * プレートが y=1415〜1471 を占め、線は y=1470 ——完全に隠れていた。画面では
 * 「A　[箱]　B」と3語が並んでいるだけに見え、矢先だけが箱の右下から
 * 覗いている状態だった。**関係を示すはずのストリップから関係が消えていた。**
 *
 * 直し方は「持ち上げ量を増やす」ではない。それだとゾーン（120px）の上端を
 * 越える。プレートの**下端を線から一定距離だけ離す**基準に変え、上限サイズは
 * 「その位置でプレートがゾーンに収まる」値から逆算する:
 * プレート高さ = `RELATION_MAX_SIZE + PLATE_PADDING_Y * 2` = 44px、
 * 下端が線の 8px 上（ゾーン下端から 78px）なので上端は 1418px——
 * ゾーン上端 1410px の内側に収まる。
 */
const RELATION_MAX_SIZE = 32;
/**
 * コネクタ（線・矢印）の幅。
 *
 * **140 → 240。** 関係ラベルは `CONNECTOR_WIDTH` からパディングを引いた幅に
 * 収まるよう縮小されるので、幅を広げないと最悪ケース（8字）のラベルが
 * 小さくなる。240 なら 8字で (240-28)/8 = 26.5px で、既知の負債として
 * 記録されている 27.5px とほぼ同じ——**線を見せるために文字を犠牲にしない**。
 * 項目ラベルに残る幅は (1080-112-240-56)/2 = 336px で、8字なら42pxまで出せる
 * （上限44には僅かに届かないが、`fitFontSize` が縮めるので溢れない）。
 */
const CONNECTOR_WIDTH = 240;
const CONNECTOR_HEIGHT = 32;
/** プレートの下端を線からどれだけ離すか。0 だと線に接して読みにくい。 */
const PLATE_LINE_CLEARANCE = 8;
/** 関係ラベルのプレートの上下パディング。高さを予測可能にするため明示する。 */
const PLATE_PADDING_Y = 6;
/** 項目とコネクタの間の隙間。 */
const GAP = 28;
/** ストリップの左右に残す余白。 */
const SIDE_PADDING = 56;
/** 関係ラベルのプレートの左右パディング。 */
const RELATION_PLATE_PADDING_X = 14;

/**
 * まっすぐな線（と、必要なら矢先）を左から右へ描き切るフラットなコネクタ。
 *
 * 以前は roughjs の手描き風コネクタ（`Rough.tsx` の `RoughConnector`）を
 * 使っていたが、restyle でスケッチ調をやめたのでここも直線に置き換える。
 * `drawProgress` に応じて `strokeDashoffset` で線を描き切る手法自体は
 * 引き継ぐ——`pathLength={1}` で正規化しているので、直線でも矢印でも
 * 同じ考え方で「線が伸びる」動きが作れる。矢印は 0→0.7 を線、0.7→1 を
 * 矢先に割り当てる（`RoughConnector` と同じ配分）。
 */
const FlatConnector: React.FC<{
  width: number;
  height: number;
  stroke: string;
  arrow: boolean;
  drawProgress: number;
}> = ({ width, height, stroke, arrow, drawProgress }) => {
  const y = height / 2;
  const headSize = 22;
  const lineEndX = arrow ? width - headSize : width - 4;
  const lineEnd = arrow ? 0.7 : 1;
  const lineProgress = Math.max(0, Math.min(1, drawProgress / lineEnd));
  const headProgress = arrow
    ? Math.max(0, Math.min(1, (drawProgress - lineEnd) / (1 - lineEnd)))
    : 0;

  return (
    <svg width={width} height={height} style={{ position: "absolute", inset: 0, overflow: "visible" }}>
      <line
        x1={4}
        y1={y}
        x2={Math.max(lineEndX, 4)}
        y2={y}
        stroke={stroke}
        strokeWidth={4}
        strokeLinecap="round"
        pathLength={1}
        strokeDasharray="1 1"
        strokeDashoffset={1 - lineProgress}
      />
      {arrow && (
        <polygon
          points={`${width - headSize},${y - 12} ${width - 4},${y} ${width - headSize},${y + 12}`}
          fill={stroke}
          opacity={headProgress}
        />
      )}
    </svg>
  );
};

/**
 * 対比（`compare`）/ 因果（`flow`）の2要素を、挿絵の下の1行だけの帯で示す。
 *
 * 以前は箱2つ＋コネクタを図の帯（720px）いっぱいに描いていたが、その帯は
 * 共有の挿絵に明け渡した。**概念（2要素とその関係）は消さず、高さだけ
 * 120pxに圧縮する**——`items` / `relation` はスキーマ上 LLM に必須で出させて
 * いるフィールドなので、描く場所を残さないとデータが腐る（誰も見ない値に
 * なる）。コネクタは `FlatConnector`（直線・矢印）を使う。
 *
 * `arrow` で `compare`（対称、方向なし＝線）と `flow`（因果、方向あり＝矢印）
 * を切り分ける。ここでは向き（水平）は固定——縦画面では1行の帯に縦の矢印は
 * 収まらない。
 */
export const RelationStrip: React.FC<{
  items: string[];
  relation: string;
  arrow: boolean;
  zone: Zone;
  frameWidth: number;
  boxA: Reveal;
  boxB: Reveal;
  relationReveal: Reveal;
}> = ({ items, relation, arrow, zone, frameWidth, boxA, boxB, relationReveal }) => {
  // 項目ラベルに使える幅。中央のコネクタとその両脇の隙間を先に引く
  // （`Compare.tsx` が箱の幅を決めていたときと同じ「中央列を先に確保する」順序）。
  const itemWidth = (frameWidth - SIDE_PADDING * 2 - CONNECTOR_WIDTH - GAP * 2) / 2;
  // **2項目のラベルは同じサイズにする。** 片方だけ大きいと対比/因果の
  // 対等な関係に見えない（`Compare.tsx` の `labelCap` と同じ理由）。
  const itemSize = Math.min(
    ...items.map((item) => fitFontSize(item, itemWidth, ITEM_MAX_SIZE)),
  );
  const relationSize = fitFontSize(
    relation,
    CONNECTOR_WIDTH - RELATION_PLATE_PADDING_X * 2,
    RELATION_MAX_SIZE,
  );

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
        gap: GAP,
      }}
    >
      {/* items はスキーマが常に2要素であることを保証しているので固定 index。 */}
      <span
        style={{
          fontFamily: FONT_STACK,
          fontSize: itemSize,
          fontWeight: 800,
          color: COLORS.accent,
          opacity: boxA.opacity,
          whiteSpace: "nowrap",
        }}
      >
        {items[0]}
      </span>
      <div
        style={{
          position: "relative",
          width: CONNECTOR_WIDTH,
          height: CONNECTOR_HEIGHT,
          opacity: relationReveal.opacity,
        }}
      >
        <FlatConnector
          width={CONNECTOR_WIDTH}
          height={CONNECTOR_HEIGHT}
          stroke={COLORS.subtle}
          arrow={arrow}
          drawProgress={relationReveal.drawProgress}
        />
        {/* **位置は「下端を線から離す」で決める。** 上端を font-size の倍数で
            持ち上げる書き方（旧実装）は、span の行ボックスが font-size より
            背が高いことを見落としており、プレートが線を完全に覆っていた
            （`RELATION_MAX_SIZE` のコメントに実測値を残した）。下端基準なら
            文字サイズが変わってもクリアランスは変わらない。
            `lineHeight: 1` を明示するのは、高さを
            `relationSize + PLATE_PADDING_Y * 2` に確定させてゾーンに収まる
            ことを計算で言えるようにするため（既定の "normal" はフォント依存で、
            ローカル Yu Gothic と本番 Noto Sans CJK で変わる）。 */}
        <span
          style={{
            position: "absolute",
            bottom: CONNECTOR_HEIGHT / 2 + PLATE_LINE_CLEARANCE,
            left: "50%",
            transform: "translateX(-50%)",
            fontFamily: FONT_STACK,
            fontSize: relationSize,
            lineHeight: 1,
            fontWeight: 900,
            color: COLORS.text,
            backgroundColor: COLORS.plate,
            padding: `${PLATE_PADDING_Y}px ${RELATION_PLATE_PADDING_X}px`,
            borderRadius: 8,
            whiteSpace: "nowrap",
          }}
        >
          {relation}
        </span>
      </div>
      <span
        style={{
          fontFamily: FONT_STACK,
          fontSize: itemSize,
          fontWeight: 800,
          color: COLORS.accent2,
          opacity: boxB.opacity,
          whiteSpace: "nowrap",
        }}
      >
        {items[1]}
      </span>
    </div>
  );
};
