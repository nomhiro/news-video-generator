import { interpolate, useCurrentFrame } from "remotion";

/**
 * シーン内の要素を「一度に」ではなく「順番に」出すためのタイミング計算。
 *
 * 図が一度に spring で出ると、それは説明ではなく単なる装飾になる。
 * ナレーションが要素に触れる順（A → 矢印 → B → 注記）と絵の出現順を
 * 揃えることで、図がナレーションの説明そのものになる。
 */

/** 1要素のアウトライン（枠線）を描き切るまでのフレーム数。 */
export const DRAW_FRAMES = 14;

/** アウトライン完了後、ハッチング塗りをフェードインさせるフレーム数。 */
export const FILL_FRAMES = 10;

/** 1要素が出現し切るまでの合計フレーム数（アウトライン＋塗り）。 */
export const REVEAL_FRAMES = DRAW_FRAMES + FILL_FRAMES;

/**
 * シーン尺のうち、要素の出現に使ってよい割合。
 *
 * 残りの40%は「完成した図をナレーションが説明する時間」。最後の要素が
 * まだ描画中にシーンが切り替わると、視聴者は完成形を一度も見られない
 * まま次のシーンに移ってしまう——図は完成した状態で止まり、
 * ナレーションが説明を続けている間その状態を保つ必要がある。
 */
const REVEAL_WINDOW_RATIO = 0.6;

/**
 * ビート1つぶんの間隔の上限（フレーム）。30fps で1秒。
 *
 * **これが無いと、要素の少ないシーンで画面が数秒間空になる。** ウィンドウを
 * 要素数で等分する式は、要素が2個（`statement` の「章タグ → 見出し」）のとき
 * 見出しをウィンドウの**末端**に置く。実測（1080x1920 / 46.2秒のショート）
 * では、
 *
 * - 6秒のフックシーン: 見出しが出るのは **2.8秒後**（尺の47%）
 * - 8秒の結論シーン: 見出しが出るのは **4.5秒後**（尺の56%）
 *
 * で、その間**画面の下半分（章タグから字幕までの650px）が空**だった。
 * 「順番に出す」演出が「何も出ない」に化けており、`statement` は
 * `compare`/`flow` よりビートが少ないぶん症状が重い。
 *
 * 上限を 30 フレームにした根拠は、**要素の多いシーンの間隔を変えないこと**。
 * 同じ動画の `compare`/`flow`（5ビート）の実測間隔は約28.6フレームで、
 * 30 を下回るのでこの上限には当たらない。ここを 12 のような小さい値にすると
 * 5ビートのシーンまで詰まり、「ナレーションが要素に触れる順に絵が出る」
 * という演出の狙い（このファイル冒頭）が失われる。
 */
const MAX_BEAT_INTERVAL = 30;

/**
 * シーン内 index 番目（0始まり）の要素が出現を開始するフレーム
 * （シーンの先頭からの相対フレーム）を返す。
 *
 * `totalBeats` 個の要素を、シーン尺の先頭 `REVEAL_WINDOW_RATIO` の中に
 * 等間隔で並べる。最後の要素の開始フレームは、自身の出現アニメーション
 * （`REVEAL_FRAMES`）を差し引いた地点に置くので、最後の要素もウィンドウ内で
 * 描き切れる。
 *
 * ただし1ビートの間隔は `MAX_BEAT_INTERVAL` で頭を打つ。ウィンドウは
 * 「ここまでに出し終える」という締切であって、「ここまで引き延ばす」という
 * 指示ではない。
 */
export function beatStart(
  index: number,
  totalBeats: number,
  durationInFrames: number,
): number {
  const window = Math.max(
    durationInFrames * REVEAL_WINDOW_RATIO - REVEAL_FRAMES,
    0,
  );
  if (totalBeats <= 1) return 0;
  const interval = Math.min(window / (totalBeats - 1), MAX_BEAT_INTERVAL);
  return Math.round(index * interval);
}

export type Reveal = {
  /** アウトライン（枠線）の描画進捗。0=未着手 / 1=描き切った。 */
  drawProgress: number;
  /** ハッチング塗りのフェード進捗。0=未着手 / 1=塗り終わり。 */
  fillProgress: number;
  /** 要素全体の不透明度（出現の最初の数フレームだけフェードイン）。 */
  opacity: number;
};

/**
 * 開始フレームから見た、いま描くべき進捗をまとめて返す。
 *
 * 各要素（ChapterTag / 箱 / 矢印など）はこれを呼ぶだけで
 * 「いつ描き始めるか」の計算を各シーンファイルに重複させずに済む。
 */
export function useReveal(startFrame: number): Reveal {
  const frame = useCurrentFrame();
  const t = frame - startFrame;
  const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;
  return {
    drawProgress: interpolate(t, [0, DRAW_FRAMES], [0, 1], clamp),
    fillProgress: interpolate(
      t,
      [DRAW_FRAMES, DRAW_FRAMES + FILL_FRAMES],
      [0, 1],
      clamp,
    ),
    opacity: interpolate(t, [0, 6], [0, 1], clamp),
  };
}
