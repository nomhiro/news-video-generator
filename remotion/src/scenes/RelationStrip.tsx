import type { Reveal } from "../beats";
import { fitFontSize } from "../fitText";
import { RoughConnector } from "../Rough";
import { COLORS, FONT_STACK } from "../theme";
import type { Zone } from "../zones";

/** 項目ラベルの上限サイズ。1行の帯なので `DiagramBox` の名札より小さい。 */
const ITEM_MAX_SIZE = 44;
/** 関係ラベルの上限サイズ。 */
const RELATION_MAX_SIZE = 38;
/** コネクタ（線・矢印）の幅。関係ラベルはこの上に重ねて浮かせる。 */
const CONNECTOR_WIDTH = 140;
const CONNECTOR_HEIGHT = 32;
/** 項目とコネクタの間の隙間。 */
const GAP = 28;
/** ストリップの左右に残す余白。 */
const SIDE_PADDING = 56;
/** 関係ラベルのプレートの左右パディング。 */
const RELATION_PLATE_PADDING_X = 14;

/**
 * 対比（`compare`）/ 因果（`flow`）の2要素を、挿絵の下の1行だけの帯で示す。
 *
 * 以前は箱2つ＋コネクタを図の帯（720px）いっぱいに描いていたが、その帯は
 * 共有の挿絵に明け渡した。**概念（2要素とその関係）は消さず、高さだけ
 * 120pxに圧縮する**——`items` / `relation` はスキーマ上 LLM に必須で出させて
 * いるフィールドなので、描く場所を残さないとデータが腐る（誰も見ない値に
 * なる）。手描き線（`Rough.tsx`）は引き続き使い、「型で作った」感を避ける。
 *
 * `arrow` で `compare`（対称、方向なし＝線）と `flow`（因果、方向あり＝矢印）
 * を切り分ける。ここでは向き（水平）は固定——縦画面では1行の帯に縦の矢印は
 * 収まらない。
 */
export const RelationStrip: React.FC<{
  items: string[];
  relation: string;
  arrow: boolean;
  seed: number;
  zone: Zone;
  frameWidth: number;
  boxA: Reveal;
  boxB: Reveal;
  relationReveal: Reveal;
}> = ({ items, relation, arrow, seed, zone, frameWidth, boxA, boxB, relationReveal }) => {
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
        <RoughConnector
          width={CONNECTOR_WIDTH}
          height={CONNECTOR_HEIGHT}
          seed={seed}
          stroke={COLORS.subtle}
          orientation="horizontal"
          arrow={arrow}
          drawProgress={relationReveal.drawProgress}
        />
        {/* 線の真上ではなく上に離して置く（線とラベルが重なる不具合は
            `Compare.tsx` の旧実装で実測済み）。帯の高さが120pxしか無いので、
            離す距離は控えめにする。 */}
        <span
          style={{
            position: "absolute",
            top: -relationSize * 1.05,
            left: "50%",
            transform: "translateX(-50%)",
            fontFamily: FONT_STACK,
            fontSize: relationSize,
            fontWeight: 900,
            color: COLORS.text,
            backgroundColor: COLORS.plate,
            padding: `2px ${RELATION_PLATE_PADDING_X}px`,
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
