import { Config } from "@remotion/cli/config";

// Linux コンテナでの安定性・速度のために公式が推奨している設定。
Config.setChromiumMultiProcessOnLinux(true);

// concurrency はここで設定しない。**Python 側が --concurrency で渡す。**
// 既定は「ホストの CPU スレッド数の半分」で cgroup を見ないため、
// 2 vCPU の割り当てに対して10スレッドが立つ（os.cpu_count() と同じ罠）。
