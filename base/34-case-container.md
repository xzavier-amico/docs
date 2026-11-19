# 34. 实例剖析：手搓一个容器——`unshare` + `cgroup` + `pivot_root`

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 操作系统：Linux 视角（实例篇） ｜ **编号**：34 / 56
> **本篇定位**：Part III 的验伪出口。15 篇说了「容器不是虚拟化，是同一内核上的三件事」，本篇把这三件事一行命令一行命令搭出来：8 个 namespace 换掉进程的解析表，cgroup v2 的一个目录限住配额，`pivot_root` 换掉根，最后 `execve`。搭完你会发现 `/proc` 里根本没有「容器」这个对象——只有几个 ns 的 inode、一行 cgroup 路径、一张挂载表。读完的判据：再听到「容器」，你能立刻把它拆成「哪几个 ns inode + 哪个 cgroup 路径 + 哪个挂载点」。
> **读法**：§34.1 先用三条真实输出打掉「容器进程去哪了」这个困惑；§34.2–§34.4 是五问前三问，其中 §34.4 的十步表是手搓的操作顺序，每步都写了「不这么做会怎样」；§34.5 是硬门槛，五组「改前/改后」全部本机实测；§34.6–§34.7 是证伪与边界。
> **不在本篇**：特权级、VM 与容器的取舍、隔离强度三角的完整推导 → `base/15`；系统调用成本模型 → `base/21`；`task_struct`/`fork`/COW → `base/23`；调度器与 `cpu.weight` → `base/25`；页表与缺页 → `base/26`；内核漏洞与逃逸 → `base/33`；沙箱四档选型 → [llm/29](../llm/29-sandbox-isolation.md)。

---

## 34.1 引子：整台机器只剩一个进程，而它就是 `ps` 自己

同一台机器、同一个 shell，相隔不到一秒的三条输出：

```
$ ps -e --no-headers | wc -l
135
$ unshare -p -f --mount-proc ps -e
    PID TTY          TIME CMD
      1 ?        00:00:00 ps
$ unshare -p -f ps -e | wc -l
136
```

第一行：135 个进程。第二行：换了 PID namespace 再挂一次 `/proc`，**整台机器只剩 1 个进程，而且就是 `ps` 自己**。第三行：同样换了 PID namespace，但没加 `--mount-proc`，`ps` 又看到 135 个（136 行减表头）。

两个最常见的困惑，一次讲清。

**「容器里进程怎么这么少？」** 一个都没少。宿主那 135 个进程照常跑、照常被调度。变的是 `ps` 的工作方式：它不扫描系统，它 `readdir("/proc")`，而 `/proc` 是**由当前 PID namespace 生成的视图**。第三行是反证——只换 ns 不重挂 `/proc`，`ps` 读到的还是宿主那张表。**namespace 换的是「名字到对象的映射」，`/proc` 是这张映射的渲染结果；两者必须一起换。**

**「宿主 `ps` 里找不到容器进程？」** 找得到，只是编号不同。`/proc/<pid>/status` 的 `NSpid` 一行同时给出两个编号：

```
$ unshare -p -f -m --mount-proc sleep 20 &
$ grep NSpid /proc/3334084/status
NSpid:	3334084	1
$ nsenter -t 3334084 -p -m ps -e
    PID TTY          TIME CMD
      1 ?        00:00:00 sleep
      2 ?        00:00:00 ps
```

**宿主看它是 3334084，它自己看自己是 1。** 同一个 `task_struct`，两套编号。

第三个困惑是「`docker ps` 有容器，为什么 `/proc` 里找不到」——因为内核里没有这个对象：`ls /proc/$$/ | grep -ci container` 返回 **0**，`/proc/$$/cgroup` 只有一行 `0::/user.slice/...`。`docker ps` 那一行是 dockerd 数据库里的记录；内核这边只有「共用哪几个 ns inode」和「挂在哪条 cgroup 路径上」。

### 本机档案与诚实声明

| 项 | 值 | 怎么得到 |
| --- | --- | --- |
| 内核 / CPU / 内存 | `6.12.113+deb12-cloud-amd64`、2 vCPU（EPYC 7K62）、1974 MiB | `uname -a`、`nproc`、`free -m` |
| 虚拟化 | KVM guest（`hypervisor` 标志位） | `systemd-detect-virt` |
| 权限 | uid 0，`CapEff: 000001ffffffffff`（41 位全 1） | `grep Cap /proc/self/status` |
| cgroup | v2 单树，8 个控制器 | `stat -fc %T /sys/fs/cgroup` |
| 容器运行时 | **没有** docker/containerd/runc/podman | `command -v` 全空 |

两件事说在前面：① 本机有完整 `CAP_SYS_ADMIN`，`unshare`/`pivot_root`/写 cgroup 全部成功；如果你的机器报 `Operation not permitted`，§34.5 末尾给了替代路径，**本篇不会把跑失败写成跑成功**。② 本机没有 Docker，§34.2 调用链上面三层来自 OCI/Docker 的公开结构，**最后一层（`clone`/`mount`/`pivot_root`/`execve`）是我用 `strace` 实测的**；`runc` 做的内核动作与 §34.4 手搓的是同一批系统调用。

> **本篇的唯一真问题**：容器不是轻量虚拟机，是**三个内核功能的组合**——命名空间（看得见什么）、cgroup（能用多少）、根切换（以为自己在哪）；没有新内核魔法，所以隔离强度也就那样。
>
> 它量到主线 ①「没有魔法，只有位与约定」：每一层都能还原成一个字段或一次系统调用；也量到主线 ③「单价 = 跨界次数 × 单次成本」：纯 ns 16.4 ms，Docker 端到端 0.3–0.8 s，差额全在包装器。

---

## 34.2 ① 它是什么：一个被换了三张表、限了配额的普通进程

| 功能 | 回答的问题 | 内核里到底是什么 | 没有它会怎样 |
| --- | --- | --- | --- |
| namespace | 我**看得见**什么 | `nsproxy` 指向的一组 `*_namespace`，每个在 nsfs 里有一个 inode | `ps` 看到 135 个进程；`ip addr` 看到宿主网卡；改 hostname 改整台机器 |
| cgroup | 我**能用多少** | `css_set` 指向 cgroup v2 树上的一个节点目录 | 一个死循环吃满整机；一次 fork 循环打满宿主 PID 表 |
| rootfs + `pivot_root` | 我以为自己**在哪** | `fs_struct->root` 指向 mnt ns 里的一个 `vfsmount` | `ls /` 看到宿主 24 个顶层目录，`/etc/shadow` 就是宿主的 |

三件事**互不依赖**：可以只做 ns 不写 cgroup（看不见外面但能吃满整机），也可以只写 cgroup 不做 ns（看得见一切但只给 0.5 核）。Docker 把它们捆在一起，是因为只有三件齐了才像一台「机器」。第四轴是**策略**（capability、seccomp、LSM），它决定「同一个 syscall 准不准发」，不改「看得见什么」——`base/15` 讲透了，本篇只在 §34.7 用它解释 `--privileged`。

### `docker run` 一层层剥下去

```
docker run -it --rm alpine sh
  ├─ docker CLI ──HTTP/JSON over docker.sock──> dockerd（API、拉镜像、建网络/volume）
  │                                              └─ gRPC ──> containerd（镜像/快照/任务生命周期）
  │                                                           └─ containerd-shim-runc-v2（每容器一个，
  │                                                              托管 stdio、回收僵尸）
  │                                                               └─ runc create + start
  │                                                                   ├─ clone(CLONE_NEW* ×8)
  │                                                                   ├─ 写 cgroup：memory.max/cpu.max/pids.max
  │                                                                   ├─ mount --make-rprivate / ; mount rootfs
  │                                                                   ├─ pivot_root + umount2(MNT_DETACH)
  │                                                                   ├─ seccomp / drop caps / no_new_privs
  │                                                                   └─ execve("/bin/sh")
  ▼
一个普通进程（宿主上比如 PID 3334084，容器里是 1）
```

链上没有一步是「虚拟化」：没有 hypervisor、没有第二个内核、没有虚拟磁盘。`CLONE_NEW*` 是 2002–2020 年陆续加的标志位，cgroup 是 2007 年的文件系统接口，`pivot_root` 2000 年就有。**容器的「新」在组合方式，不在任何一项技术是新的。**

### 实测：最后一层只有 5 个系统调用

```
$ strace -f -qq -e trace=clone,unshare,mount,pivot_root,execve \
    unshare -p -f -m -u -i -n --mount-proc /bin/true
execve("/usr/bin/unshare", [...]) = 0
unshare(CLONE_NEWNS|CLONE_NEWUTS|CLONE_NEWIPC|CLONE_NEWPID|CLONE_NEWNET) = 0
clone(child_stack=NULL, flags=CLONE_CHILD_CLEARTID|CLONE_CHILD_SETTID|SIGCHLD, ...) = 3322660
[pid 3322660] mount("none", "/", NULL, MS_REC|MS_PRIVATE, NULL) = 0
[pid 3322660] mount("proc", "/proc", "proc", MS_NOSUID|MS_NODEV|MS_NOEXEC, NULL) = 0
[pid 3322660] execve("/bin/true", ["/bin/true"], ...) = 0
```

三件事：① 一次 `unshare()` 传 5 个 `CLONE_NEW*`，不是「创建容器」，是「换 5 张表、让后续 `clone` 的子进程继承」；② 子进程先 `mount --make-rprivate` **关掉挂载传播**，否则挂 `/proc` 会传播回宿主；③ **trace 里没有 `pivot_root` 也没有 cgroup**——`unshare` 只换表。真正的容器 = 这条 trace + 一个 `pivot_root` + 三次 `write`。

### 三层包装器各管什么

`dockerd` 管 API 与镜像/网络/volume，`containerd` 管镜像快照与任务生命周期，`containerd-shim` 每容器一个常驻进程托管 stdio、回收僵尸，`runc`/`crun` 只负责读 OCI `config.json` 并按规范把进程造出来。**低层运行时的接口就是「一个 bundle 目录 + 一次 exec」**，所以 `crun` 能替 `runc`、`kata` 能替 `runc`（换成 microVM）。拿掉 `runc` 这一层，就得自己写 §34.4 的代码——本机用 `unshare` + `mini.c` 替的就是它。

---

## 34.3 ② 物理形态：八张表、一棵树、一个根

### 八类 namespace

| ns | `clone` 标志 | 换掉的是 | 一个反直觉的点 |
| --- | --- | --- | --- |
| mnt | `CLONE_NEWNS` | 挂载表（`mountinfo`） | 不隔离文件本身：同一 inode 可在两个 ns 以不同路径出现；已打开的 fd 不受约束 |
| pid | `CLONE_NEWPID` | PID 编号与 `/proc` 进程目录 | **只影响之后 `fork` 的子进程**，所以必须 unshare 后立刻 fork |
| net | `CLONE_NEWNET` | 网卡、路由、端口、iptables、conntrack | 不隔离带宽（那是 cgroup），也不隔离软中断占的 CPU |
| uts | `CLONE_NEWUTS` | `uname()` 的 nodename/domainname | 新 ns **继承**旧名字，不是清空；改的只是名字 |
| ipc | `CLONE_NEWIPC` | SysV shm/sem/msg、POSIX mq | 已 `mmap` 的共享页不会因换 ns 消失；隔离发生在「创建/查找 key」 |
| user | `CLONE_NEWUSER` | uid/gid 映射表 + **cap 作用域** | 让非 root 也能建其它 ns，是 rootless 的地基 |
| cgroup | `CLONE_NEWCGROUP` | `/sys/fs/cgroup` 的**可见根** | 只改「看见哪棵子树」，不改配额；看不见不等于没有 |
| time | `CLONE_NEWTIME` | `CLOCK_MONOTONIC`/`BOOTTIME` 偏移 | 不影响 `CLOCK_REALTIME`（墙上时钟全局）；给「迁移后 uptime 不跳」用 |

`pid_for_children`/`time_for_children` 不是第九、十类，是**「下一次 `fork` 会进哪个 ns」的待生效指针**。`unshare -p` 之后当前进程的 `pid` 链接不变、`pid_for_children` 变，就是这个原因。

### `/proc/self/ns/*` 的 inode 是什么

`pid:[4026531836]` 不是「编号」，是 **nsfs 里的 inode 号**。它的意义只有一条，但极其有用：**两个进程在不在同一个 ns，等价于 `/proc/<pid>/ns/<type>` 的 inode 是否相同。** 这个 inode 还能被 `open()` 成 fd，`setns` 加入；而且只要还有 fd 或 bind mount 指着它，**ns 就不会死**，哪怕里面一个进程都没有。`ip netns add` 就是把 netns bind mount 到 `/var/run/netns/<name>` 来保活。

本机实测（宿主 vs `unshare -p -f -m -u -i -n` 的子进程）：

| ns | 宿主 inode | 新 inode | 变了吗 |
| --- | --- | --- | --- |
| pid | `4026531836` | `4026532419` | ✅ |
| mnt | `4026531841` | `4026532416` | ✅ |
| net | `4026531840` | `4026532420` | ✅ |
| uts | `4026531838` | `4026532417` | ✅ |
| user | `4026531837` | `4026531837` | ❌ |
| cgroup | `4026531835` | `4026531835` | ❌ |

**你不 unshare 的 ns，它就不会变。** 很多人以为「进了容器就自动有独立 user ns」，其实 Docker **默认不启用 user namespace remapping**——容器里是 uid 0，宿主眼里也是 uid 0，只是 cap 被削、seccomp 装上了。§34.6 会把这句话变成一条被证伪的说法。

### cgroup v2：一棵树，一组文件，一次迁移

```
/sys/fs/cgroup/                  ← cgroup2fs，整机只有这一棵
├── cgroup.controllers           cpuset cpu io memory hugetlb pids rdma misc
├── cgroup.subtree_control       ← 往子层「下发」哪些控制器
├── cgroup.procs                 ← 写 PID = 把进程搬进这个组
└── probe34/                     ← 我手建的「容器」
    ├── memory.max   67108864    ← 硬上限 64 MiB，回收不动就 OOM
    ├── memory.high  （未设）     ← 软上限，只限速不杀
    ├── memory.current / peak    ← 当前用量 / 历史峰值
    ├── memory.events            ← low/high/max/oom/oom_kill 计数
    ├── cpu.max      "50000 100000"  ← 每 100 ms 最多 50 ms CPU = 0.5 核
    ├── cpu.weight   100         ← 有竞争时的相对权重（不是上限）
    ├── pids.max     16          ← 组内任务数上限
    └── pids.events  max 1       ← 被拒了几次
```

四条规则：① **单树**——一个进程在每条 controller 上只有一条路径（v1 多树会让内存按 A 算、CPU 按 B 算）。② **控制器先下发**——父层 `cgroup.subtree_control` 没开的，子层连文件都不出现。③ **no internal process**——有子组的组不能再放进程，配额写在叶子上。④ **`cgroup.procs` 是迁移**——搬进去那一瞬配额才生效；可热迁移，但 page cache 归属不跟着搬。

### rootfs：镜像不是「一个文件系统」

```
镜像（只读，OCI layer tar）            容器可写层
┌──────────────┐
│ layer3: app  │──┐
│ layer2: deps │──┼──> overlayfs ──> merged（一棵普通目录树）
│ layer1: base │──┘        ▲              │
└──────────────┘     upperdir+workdir     ▼
                    （写时复制）      pivot_root(merged, merged/.pivot)
                                     umount2("/.pivot", MNT_DETACH)
```

- **镜像**是若干层 tar + 一个 `config.json`。`docker save` 出来是 tar，没有分区表、没有文件系统超级块。
- **可写层**是 overlayfs 的 `upperdir`。第一次改下层文件触发 **copy-up**：整个文件复制上来再改副本。
- **rootfs** 是联合挂载后的 `merged` 树，经 `pivot_root` 变成进程眼中的 `/`。

三个证据：

```
$ ./mini $R /bin/sh -c 'uname -r; df -h /'
6.12.113+deb12-cloud-amd64
/dev/vda1      19.7G  9.8G  9.0G  52% /
```

`uname -r` 是**宿主内核**；`df /` 是**宿主同一个 `/dev/vda1`**（没建任何块设备）；`/` 只有 6 个条目。我的 rootfs 只有 2.9 MB、22 个目录项——如果镜像是「一个文件系统」，它至少得有完整的 `/usr`、`/etc`。

### `pivot_root` 与 `chroot` 的物理差别

`chroot` 只改 `fs_struct->root` 指针，**不动 mount ns 的根挂载**：已打开的 fd 仍指向旧根的文件，`/proc/1/root` 还能绕回去，其他进程完全不受影响。

`pivot_root(new_root, put_old)` 换的是**整个 mount ns 的根挂载**：旧根被移到 `put_old`，再 `umount2(put_old, MNT_DETACH)` 摘掉。摘掉后旧根**没有任何路径可达**（除非有人提前留了 fd）。实测：

```
[before] host / inode   = 2
[before] new-root inode = 428946
[after ] / inode        = 428946
--- ls / ---  bin  dev  lib  lib64  oldroot  proc
--- /oldroot 条目 --- 24
--- after umount -l --- 0
--- ls /etc --- No such file or directory
```

`/` 的 inode 从 `2`（宿主 ext4 根）变成 `428946`，`/oldroot` 从 24 个宿主目录变成 0，`/etc` 直接不存在。**「以为自己在哪」是靠换掉 `vfsmount` 指针实现的，不是靠藏文件。**

---

## 34.4 ③ 走一遍：十步，每步都知道「不这么做会怎样」

| # | 做什么 | 关键命令 | 不这么做会怎样 |
| --- | --- | --- | --- |
| 1 | 换 5 张表 | `unshare -p -f -m -u -i -n` | `ps` 看到宿主 135 个；`ip addr` 看到 `eth0` |
| 2 | 关挂载传播 | `mount --make-rprivate /` | 挂 `/proc` 会传播回宿主，把宿主 `/proc` 盖成新视图 |
| 3 | 挂 `/proc` | `mount -t proc proc /proc` | 实测 `ps -e` 输出 136 行，新 pid ns 白建 |
| 4 | 准备新根 | `mount --bind $ROOT $ROOT` | `pivot_root` 要求 new_root 是挂载点，否则 `EINVAL` |
| 5 | 换根 | `pivot_root($ROOT, $ROOT/oldroot)` | 用 `chroot` 代替：旧根仍可达，fd 能读宿主文件 |
| 6 | 摘旧根 | `umount2("/oldroot", MNT_DETACH)` | `/oldroot` 仍有 24 个宿主目录，`/etc` 可读 |
| 7 | 放进 cgroup | `echo $$ > .../cgroup.procs` | 配额写得再对也不生效——限额作用于组 |
| 8 | 写配额 | `memory.max`/`cpu.max`/`pids.max` | 不限内存时 200 MiB 分配成功；不限 CPU 时 2 s 用 1.87 s |
| 9 | 削权限 | drop caps + seccomp + `no_new_privs` | 容器 root 能 `mount`/`mknod`/改 `sysctl` |
| 10 | 换程序 | `execve("/bin/sh")` | 1 号是 `unshare` 自己，不是你的服务 |

### 第 1–3 步：换表、关传播、重挂 `/proc`

```bash
unshare --pid --net --mount --uts --ipc --fork --mount-proc \
        /bin/sh -c 'echo pid=$$; hostname; ip -o addr show'
```

三个细节各能坑一天：**`--fork` 不能省**——`unshare(CLONE_NEWPID)` 只改「未来孩子的 pid ns」，调用者留在旧 ns；**`--mount-proc` 隐含 `--mount`**，util-linux 会先 `mount --make-rprivate /` 再挂 `/proc`；**手动挂时必须先 `--make-rprivate`**，否则在共享 mnt ns 里 `mount -t proc proc /proc`，宿主所有 `ps`、`top`、监控 agent 瞬间只看到 1 个进程，直到该 mount ns 销毁。

### 第 4–6 步：换根，并把旧根真正摘掉

```bash
R=/tmp/opencode/root34          # 2.9 MB：busybox + libc + ld.so + 几个 symlink
mkdir -p $R/{bin,proc,dev,lib,lib64,oldroot}
mount --bind $R $R              # pivot_root 前提：new_root 必须是挂载点
pivot_root $R $R/oldroot        # 旧根被移到 $R/oldroot
cd /
umount -l /oldroot              # MNT_DETACH：从树上摘掉，不再有路径可达
mount -t proc proc /proc        # 新 pid ns 的进程视图
exec /bin/sh
```

`pivot_root` 四个约束常被忽略：两个路径必须在**同一个 mount ns**；`new_root` 必须是挂载点；`put_old` 必须在其下；调用者的 `pwd` 不能在旧根里（先 `cd /`）。任一不满足都是 `EINVAL`，而 `EINVAL` 不告诉你哪条。**为什么必须 `MNT_DETACH`？** 普通 `umount` 会因「还有进程 cwd/root 在旧根」返回 `EBUSY`；`MNT_DETACH` 是「立刻从命名空间摘掉，最后一个引用消失再真正释放」。摘掉后旧根在路径上不存在，但**如果有人在 pivot 前打开了旧根里的 fd，那个 fd 仍然有效**——§34.7 的第一条逃逸面。

### 第 7–10 步：限额、削权、换程序

```bash
CG=/sys/fs/cgroup/probe34
mkdir -p $CG
echo 64M > $CG/memory.max;  echo 0 > $CG/memory.swap.max
echo "50000 100000" > $CG/cpu.max        # 0.5 核
echo 16 > $CG/pids.max
echo $$ > $CG/cgroup.procs               # 迁移：从这一秒起配额生效
```

顺序很重要：**先建组、写限额，再把进程搬进去**。反过来也能用，但中间那一小段是「无限制」窗口；对不可信代码，这个窗口够跑完一次 fork bomb。然后是最容易被跳过、也最不该跳过的一步：**`execve` 前削权限**。Docker 默认会把 cap 从 41 位削到十几位、装 seccomp 白名单（约 300 个 syscall 禁掉 40 多个）、设 `no_new_privs`、只读挂载关键路径。少了这些，前面九步只是「一个看不见外面的 root」——它仍然能 `mount`、`mknod`、加载模块，隔离强度等于零。

### 真的手搓：27 行 C

`unshare` 做不了 `pivot_root` 和 cgroup 写入。把三件事写进一个程序，就是最小容器运行时（本机编译运行通过）：

```c
#define _GNU_SOURCE
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/mount.h>
#include <sys/syscall.h>
#include <sys/wait.h>

/* mini <newroot> <cmd...> : 只做三件事——ns / rootfs / exec */
int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: %s NEWROOT CMD...\n", argv[0]); return 2; }
    if (unshare(CLONE_NEWNS | CLONE_NEWPID | CLONE_NEWUTS |
                CLONE_NEWIPC | CLONE_NEWNET) < 0) { perror("unshare"); return 1; }
    pid_t p = fork();                       /* 新 pid ns 里自己是 1 号 */
    if (p > 0) { waitpid(p, NULL, 0); return 0; }

    mount("none", "/", NULL, MS_REC | MS_PRIVATE, NULL);  /* 别把挂载传回宿主 */
    mount(argv[1], argv[1], NULL, MS_BIND, NULL);         /* new_root 必须是挂载点 */
    if (syscall(SYS_pivot_root, argv[1], "/tmp/opencode/root34/oldroot") < 0) {
        perror("pivot_root"); return 1;
    }
    chdir("/");
    umount2("/oldroot", MNT_DETACH);        /* 断开旧根，逃逸通道关闭 */
    mount("proc", "/proc", "proc", 0, NULL);
    execv(argv[2], &argv[2]);
    perror("execv");
    return 1;
}
```

```
$ gcc -O2 -o mini mini.c && ./mini $R /bin/sh -c '
    echo "pid=$$ uts=$(hostname)"; hostname mini34; echo "uts -> $(hostname)"
    ls /; echo "oldroot=$(ls /oldroot | wc -l)"; ps; ip -o addr show | wc -l'
pid=1 uts=LegendVPS-20260712140708acd8
uts -> mini34
bin  dev  lib  lib64  oldroot  proc
oldroot=0
PID   USER     COMMAND
    1 0        /bin/sh -c ...
   12 0        ps
0
$ hostname
LegendVPS-20260712140708acd8
```

七行输出把三件事全证了：`pid=1`（新 pid ns）、`uts -> mini34` 而宿主不变（uts 生效）、`/` 只有 6 个条目且 `oldroot=0`（新根 + 旧根已摘）、`ps` 2 个进程（proc 重挂）、地址 0 个（新 net ns 未配置）。**这就是一个容器**——没有 Docker、没有镜像格式，只有 27 行 C 和一次 `execve`。

### 成本：手搓 16.4 ms，Docker 端到端 0.3–0.8 s

```
$ time (for i in $(seq 100); do unshare -p -f true; done)
real    0m1.644s        # 16.4 ms / 次
```

这 16.4 ms 是端到端：`sh` fork 出 `unshare`、一次 `unshare()` + 一次 `clone()`、子进程 `execve(true)`、两次进程销毁。真正的 `unshare()` 只有几十微秒——**大头是两次 `fork`+`exec`**（`base/15` 实测 fork ≈ 0.1 ms、exec ≈ 0.45 ms）。与 `docker run` 的 0.3–0.8 s 差 20–50 倍，差额全在包装器：镜像解压、overlay 挂载、veth/bridge、iptables、gRPC、日志驱动。**容器启动慢，慢在包装器，不慢在 namespace。**

---

## 34.5 ④ 改一行：五组「改前/改后」，本机实测

先探测权限，决定这节能写到哪一步：

```
$ grep Cap /proc/self/status
CapEff: 000001ffffffffff      ← 41 位全 1，CAP_SYS_ADMIN 在
$ unshare -p -f --mount-proc /bin/echo ok
ok                            ← namespace 可创建，实验全部真跑
```

### A. 加一个 `--mount-proc`：135 → 1

| 改法 | 容器里 `ps` 看到的进程数 | 说明 |
| --- | --- | --- |
| 不改（宿主） | **135** | 基线 |
| 只 `unshare -p -f` | **135**（136 行） | 换了 pid ns，`/proc` 还是宿主的表 |
| 加 `--mount-proc` | **1**（`ps` 自己） | 换了表，也重挂了渲染器 |

**一行之差，67 倍。** 不是「进程被藏起来」，是「`ps` 换了一本花名册」。`/proc` 里 135 个目录一个没消失，宿主 `ps` 照样数得出来；变的只是「从哪个 pid ns 的角度渲染」。

### B. 写 `memory.max`：40 MiB 通过，200 MiB 被 SIGKILL

| # | 配置 | 动作 | 结果 | `memory.peak` | `memory.events` |
| --- | --- | --- | --- | --- | --- |
| 1 | 无 cgroup | 分配 200 MiB 并 touch | ✅ 成功 | — | — |
| 2 | `memory.max=64M` | 分配 40 MiB 并 touch | ✅ 成功 | `45,109,248` B（43.0 MiB） | 全 0 |
| 3 | `memory.max=64M` | `mmap` 200 MiB，**不 touch** | ✅ 成功 | `3,268,608` B（3.1 MiB） | 全 0 |
| 4 | `memory.max=64M` | 分配 200 MiB 并 touch | ❌ **SIGKILL(137)** | `67,108,864` B（64.0 MiB） | `max 127`、`oom_kill 1` |

第 4 组的真实输出：

```
$ echo 64M > /sys/fs/cgroup/probe34mem/memory.max
$ sh -c 'echo $$ > /sys/fs/cgroup/probe34mem/cgroup.procs; exec python3 -c "
    b=[]
    for i in range(200):
        x=bytearray(1024*1024)
        for j in range(0,len(x),4096): x[j]=1
        b.append(x)"'
Killed
EXIT=137
$ cat .../memory.peak
67108864
$ cat .../memory.events
max 19
oom 1
oom_kill 1
```

三个数字逐位看：**`peak` 正好等于 `memory.max`**——内核在「再分配一页就超限」那一刻触发回收，回收不到就 OOM kill，不会「先超一点再杀」；**`max 19` 是撞上限次数**，不是杀进程数，`oom_kill 1` 才是；**第 3 组与第 4 组只差「touch」**——`mmap` 200 MiB 只改页表、记一个 VMA，物理页一页没分配，cgroup 限的是 **RSS + page cache**，不是虚拟地址空间。这就是「`memory.max` 明明写了，为什么还能 `mmap` 几个 G」的答案：**虚拟内存是承诺，cgroup 管的是兑现。** `memory.high` 与 `max` 的区别也在此：前者超了只限速 + 强制回收，进程变慢但不死。

### C. `unshare -n`：5 个地址 → 0 个 → 2 个

| 状态 | 地址数 | `lo` | 能干什么 |
| --- | --- | --- | --- |
| 宿主 net ns | **5** | UP | 连外网、bind 宿主端口 |
| 新 net ns，未配置 | **0** | **DOWN** | 什么都不行——连 `127.0.0.1` 都不通 |
| 新 net ns + `ip link set lo up` | **2** | UP | 只能回环；没有路由/DNS/网关 |

```
$ ip -o addr show | wc -l
5
$ unshare -n sh -c 'ip -o addr show | wc -l'
0
$ unshare -n sh -c 'ip link set lo up; ip -o addr show'
1: lo    inet 127.0.0.1/8 scope host lo ...
1: lo    inet6 ::1/128 scope host ...
```

**「隔离了网络」不等于「安全」，也不等于「断网」。** 新 net ns 里连 `lo` 都是 DOWN，默认确实出不去；但配上 veth + bridge + NAT（Docker 默认就这么做），容器立刻能上网。反过来，默认 bridge 网络**允许容器访问宿主能访问的一切**（包括云元数据 `169.254.169.254`），除非显式配 `--internal` 或网络策略。netns 给的是「一张独立网络栈」，不是「一条出口策略」。

### D. user namespace：`0:42` → `1000:65534`

```
$ ls -ln /etc/shadow
-rw-r----- 1 0 42 651 /etc/shadow
$ unshare -U --map-user=1000 --map-group=1000 sh -c 'id; cat /proc/self/uid_map; ls -ln /etc/shadow; chown 0:0 /tmp/x; setpriv --reuid=0 id'
uid=1000 gid=1000 groups=1000
      1000          0          1
-rw-r----- 1 1000 65534 651 /etc/shadow
chown: ... Invalid argument
setpriv: setresuid failed: Invalid argument
```

`uid_map` 的 `1000 0 1` 读法是「ns 内 1000 ← 外面 0，长度 1」：**只有这一个 uid 合法**，其余全溢出成 65534（所以 gid 42 显示为 65534，`chown 0`/`setuid(0)` 报 EINVAL）。这就是 rootless 容器的物理基础——普通用户 uid 1000 建 userns 映射成 ns 内 0，在 ns 里有全部 cap，宿主眼里始终是 1000。

### E. `pivot_root`：`/` 的 inode 从 2 变成 428946

| 观察点 | 宿主 | 容器内 |
| --- | --- | --- |
| `/` 的 inode | `2` | `428946` |
| `/` 顶层条目 | 24 | **6** |
| `/oldroot` 条目 | 24 | **0**（`umount -l` 后） |
| `/etc/shadow` | 可读 | `No such file or directory` |
| `uname -r` | `6.12.113+...` | **完全相同** |
| `df /` 的块设备 | `/dev/vda1` 19.7 G | **同一个** |

最后两行是「不是 VM」的硬证据，前四行是「根切换真的发生」的硬证据：**同一个内核、同一个块设备，但 `/` 是另一个 inode。**

### 跑不出来怎么办：没有 `CAP_SYS_ADMIN` 时的替代

受限环境（CI 默认 runner、未加 privileged 的 Pod）里 `unshare -p` 返回 `EPERM`。**不要伪造输出**，按三条走：① **能验证（只读 `/proc`）**——`ls -l /proc/self/ns/` 拿 8 个 inode，`cat /proc/self/uid_map`、`/proc/self/cgroup`、`/sys/fs/cgroup/.../memory.max`，不需要任何权限，能证明「我就在某个 ns + 某条 cgroup 里」。② **能对比（找一个已存在的 ns）**——有 Docker 时 `docker inspect -f '{{.State.Pid}}'` 拿宿主 PID，与自己的比 inode，`grep NSpid` 看两套编号，这一步不需要 root。③ **只能推演**——`pivot_root` 后 `/` inode 变化、`umount -l` 后 `/oldroot` 归零、`peak` 精确等于限额，这些必须真跑；跑不了就写「本机无权限，以下为基于内核文档的推演」并给出可复现命令。判据：**任何数字，要么有命令、要么有出处、要么标推演。**

---

## 34.6 ⑤ 它证伪了谁：四条流行说法，四条反证

### ① 「容器是轻量级虚拟机」——它连内核都没有

```
$ uname -r                        # 宿主
6.12.113+deb12-cloud-amd64
$ ./mini $R /bin/sh -c 'uname -r' # 容器内
6.12.113+deb12-cloud-amd64
```

版本号一模一样。容器调用宿主的 `syscall`、跑宿主的调度器、用宿主的页表机制。所以内核漏洞不是「容器内提权」，是**直接到宿主**——容器里的 root 本来就在宿主内核里，缺的只是一个能让代码跑在 CPL=0 的 bug。VM 的边界是 hypervisor + 第二个内核；容器的边界是**一组指针**（nsproxy、css_set、fs_struct）。前者要模拟硬件，后者只换表——这就是 10 倍启动差、100 倍密度差的来源。

### ② 「容器里的 root 不是 root」——只对一半，另一半更危险

**启用 userns remap 时**：容器内 uid 0 被映射到宿主的非 0 uid。本机实测映射 `1000 0 1`，宿主 root 的文件在容器里显示成 `1000:65534`，`chown 0` 直接 `EINVAL`。这时这句话成立。

**没启用 userns remap 时（Docker 默认）**：容器内 uid 0 **就是宿主 uid 0**。`id` 看到 `uid=0`，宿主 `/proc/<pid>/status` 里也是 `Uid: 0 0 0 0`。它之所以「不像 root」，是因为 `CapEff` 被削到十几位、seccomp 装上了、`no_new_privs=1`。**这不是「不是 root」，是「root 被绑住了手脚」。**

这个区别决定 `--privileged` 为什么是灾难：它做的就是把绳子全解开——41 位 cap 全给、关 seccomp/AppArmor、映射宿主全部设备。此时容器里是**货真价实的宿主 root**，只剩一个 mnt ns 挡着。准确的说法是：**容器的 root 是一个「作用域被缩小、能力被削减」的 root；默认 uid 仍是宿主 0。**

### ③ 「容器隔离了网络」——只给了你一张独立网络栈

`unshare -n` 后地址 0 个、`lo` DOWN、没路由没 DNS，看起来隔离得很彻底，但这只是**未配置**，不是**不可配置**：

| 你以为 | 实际 |
| --- | --- |
| 容器默认断网 | Docker 默认 bridge + NAT，**能上外网** |
| 容器不能访问宿主 | 默认网关就是宿主 bridge |
| 容器不能访问内网 | 除非 `--internal`/网络策略，否则**能访问宿主能访问的一切** |
| 容器之间互相隔离 | 同一 bridge 上**默认互通** |

netns 隔离的是**网络栈**（网卡、路由、端口、iptables、conntrack），不是**可达性**。可达性由 veth/bridge/NAT/防火墙策略决定，全是「配置」。**把「有独立 netns」当成「网络安全」，是容器安全里最常见的错觉。**

### ④ 「镜像就是文件系统」——分层 tar + 联合挂载

三个反证：`df /` 显示的是宿主 `/dev/vda1`（没有自己的块设备、没有自己的超级块，`mkfs` 一次都没发生）；手搓的 rootfs 只有 2.9 MB、22 个目录项却能跑 shell；`/` 的 inode 是 428946 而宿主是 2（换的是挂载点，不是介质）。

镜像的真实结构是：**若干只读层 tar + 一个 JSON 配置 + 一个可写层**，overlayfs 联合挂载成一棵树。三个工程后果：**层是共享的**（100 个容器用同一 base，磁盘和 page cache 都只有一份——这是密度优势，也是侧信道）；**写时复制**（改下层文件要先 copy-up，第一次写可能拷整个文件）；**镜像不是不可变的**（`docker commit` 把可写层固化成新层，「同一镜像 ID」在不同机器上可能对应不同层集合——这是「摘要 vs 标签」问题的物理来源）。

---

## 34.7 边界：容器挡不住什么，以及三角表

**① 内核漏洞。** 所有容器共享同一份内核代码段。一个 `io_uring` 的 UAF、一个 `nf_tables` 越界写，在容器模型里是**直接到宿主**。防御只有三条：打补丁（紧迫性等于所有租户的安全）、缩小攻击面（seccomp 白名单、drop caps、只读 rootfs、禁 `ptrace`/`mount`）、换边界（gVisor/microVM）。注意 seccomp 只能拒绝**整次系统调用**，不能检查指针内容——它是「缩小攻击面」，不是「建立隔离」。

**② 共享内核的侧信道。** 隔离的是「名字」，不是「物理资源」：容器共享 CPU 缓存、页缓存、内存带宽、NUMA。页缓存探测能判断「某文件存不存在」——即使没权限读；CPU 缓存争用能推断邻居的访问模式；`memory.current`、`cpu.stat`、`pids.current` 在容器内可读，能推断同宿主其他租户的活动节律。这不是配置错了，是共享物理资源的必然结果；要挡住只能换边界（独占核/缓存/页缓存），也就是 VM 或物理机。

**③ 错误挂载——逃逸最常见的原因，不是 0day。**

| 错误挂载 | 后果 |
| --- | --- |
| `-v /var/run/docker.sock:...` | 容器内可创建**新的特权容器**，等于宿主 root |
| `-v /:/host`、`-v /proc:/host/proc` | 直接读写宿主文件系统/进程内存 |
| `--privileged` | 全部 cap + 全部设备 + 关 seccomp，等于没隔离 |
| 遗留 fd（pivot 前打开的） | 旧根摘掉后，fd 仍能读写宿主文件 |

**④ 配置错误——默认值不是安全值。** Docker 默认 seccomp 白名单挡不住「合法 syscall 的滥用」，默认 cap 集合仍含 `CAP_NET_RAW`（能发原始包）、`CAP_CHOWN`、`CAP_SETUID`。**「能跑起来」和「隔离住了」是两件事。**

```
┌────────────── 宿主内核（唯一的一份）──────────────┐
│ 调度器 · 页表 · VFS · 网络栈 · 驱动 · 全部 syscall │
│  ┌─ A ─────┐  ┌─ B ─────┐  ┌─ C ─────┐          │
│  │nsproxy  │  │nsproxy  │  │nsproxy  │          │
│  │css/root │  │css/root │  │css/root │          │
│  └─ 边界在 syscall 入口 ─┘  │          │          │
└──────▲───────────▲───────────▲───────────────────┘
  一次 syscall 之后，三者跑同一片内核代码、同一批物理页、同一个缓存
```

| 档位 | 边界由谁实现 | 启动 | 额外开销 | 64c/512G 密度 | 挡不住 |
| --- | --- | --- | --- | --- | --- |
| 裸进程 | 无（只有 uid/cap） | 0.1–0.5 ms | 1–10 MiB | 10³–10⁴ | 一切 |
| **容器（本篇）** | 宿主内核 + ns/cgroup | **16.4 ms**（实测）；`docker run` 0.3–0.8 s | 与进程同量级 + 镜像页缓存 | 2 核配额 32；0.5 核 100+ | 内核漏洞、侧信道、错误挂载 |
| gVisor | 用户态内核拦 syscall | 0.3–1.5 s | +20–100 MiB | 30–80 | Sentry 自身漏洞、syscall 密集变慢 |
| microVM | hypervisor + 独立 guest 内核 | 60–150 ms；快照 5–30 ms | +5 MiB VMM + 30–50 MiB guest | 按 2 核 32 | hypervisor 漏洞 |
| 物理机 | 固件 + 网闸 | 30 s–数分钟 | 0 | 1 | 供应链、BMC |

（容器档 16.4 ms 为本机实测，其余取自 `base/15` 与 [llm/29](../llm/29-sandbox-isolation.md) 的同一套口径，属量级结论。）

真正的判据是三个**乘积项**：`实际安全性 = 隔离强度 × 配额完整性 × 出口收敛性`。任一为 0，整体为 0：microVM 不写 `pids.max` 照样被 fork bomb 打穿；gVisor 能自由出网照样能把数据送出去。**这是乘法，不是加法**——也是为什么「建 ns」「写 `memory.max`」「写 `pids.max`」要分开讲：它们各自只买一项，谁也不替谁兜底。

---

## 34.8 五问收口

| 五问 | 本篇的答案 | 关键数字 |
| --- | --- | --- |
| **① 它是什么** | 普通进程 + 被换掉的 nsproxy + 一条 cgroup 路径 + 被换掉的 `fs_struct->root`。「容器」是用户态工具给这组对象起的名字 | `/proc/$$/` 里 `grep -ci container` = **0** |
| **② 物理形态** | 8 类 ns 各有独立 inode（同 inode = 同 ns，fd 可保活）；cgroup v2 单树，配额写在叶子；rootfs 是 overlay 的 merged 目录，`pivot_root` 换 `vfsmount` 指针 | inode `4026531836` → `4026532419`；`memory.max` 一行 64 MiB |
| **③ 走一遍** | `unshare(5 个 CLONE_NEW*)` → `fork` → `mount --make-rprivate` → `mount proc` → `pivot_root` → `umount2(MNT_DETACH)` → 写 3 个 cgroup 文件 → `execve` | 纯 ns 16.4 ms/次；Docker 端到端 0.3–0.8 s |
| **④ 改一行** | `--mount-proc`：135 → 1；`memory.max=64M`：200 MiB → SIGKILL 137，`peak` 67,108,864；`unshare -n`：5 → 0 个地址；`--map-user=1000`：`0:42` → `1000:65534`；`pivot_root`：inode 2 → 428946 | 五组见 §34.5 |
| **⑤ 它证伪了谁** | ① 不是轻量 VM（`uname -r` 相同、共享 `/dev/vda1`）；② 「容器 root 不是 root」只对一半（默认 uid 仍是宿主 0）；③ netns ≠ 网络策略（默认 bridge + NAT 能出网）；④ 镜像 ≠ 文件系统（分层 tar + overlay） | §34.6 四条反证 |

---

## 34.9 一页速记

```
【一句话】
容器 = 普通进程 + 8 张表里换掉几张 + 一条 cgroup 路径 + 一个被换掉的根。
内核里没有 "container" 这个对象；/proc 里只有 ns inode、cgroup 路径、挂载表。

【五个数字】
  宿主 ps -e          = 135；容器里 = 1（ps 自己）；不加 --mount-proc 仍是 135
  memory.max=64M      = peak 精确 67,108,864；200 MiB touch → SIGKILL 137
  cpu.max="20000 100000" = 2 s 墙钟只跑 0.43 s CPU（0.2 核）
  纯 ns+cgroup+exec   = 16.4 ms；docker run 端到端 0.3–0.8 s
  pids.max=16         = 第 17 次 fork 返回 EAGAIN，不是 OOM

【三个 inode】
  同 inode = 同 namespace；fd 能保活一个没有进程的 ns（ip netns 就这么做）
  host 4026531836 → new 4026532419；不 unshare 的 user/cgroup/time 不变

【一个根】
  pivot_root 换的是 vfsmount 指针，不是介质：/ inode 2 → 428946，/dev/vda1 不变
  必须 umount2(put_old, MNT_DETACH)，否则 /oldroot 里 24 个宿主目录仍可读

【三条不能省的顺序】
  1. unshare 之后必须 fork —— CLONE_NEWPID 只改"未来的孩子"
  2. 挂 /proc 之前必须 mount --make-rprivate / —— 否则传播回宿主
  3. 写限额前后必须写 cgroup.procs —— 限额作用于组，不作用于路径

【四句被证伪的话】
  "容器是轻量 VM"        → uname -r 相同，共享宿主内核与 /dev/vda1
  "容器里 root 不是 root" → 默认 uid 仍是宿主 0；被削的是 cap，--privileged 全解开
  "容器隔离了网络"        → netns 只给独立网络栈；可达性靠 veth/NAT/策略
  "镜像就是文件系统"      → 分层 tar + overlay；df 看到的是宿主盘

【一条乘法】
  实际安全性 = 隔离强度 × 配额完整性 × 出口收敛性
  任一项为 0，整体为 0：microVM 不写 pids.max 照样被 fork bomb 打穿
```

---

## 34.10 自检 3 题

**Q1.** 宿主 `ps -e` 有 135 个进程，`docker exec` 进容器后只剩 2 个。这 133 个去哪了？给出**两条命令**证明「容器里的 1 号」与「宿主某个 PID」是同一个进程。

<details><summary>答案</summary>

一个都没消失。`ps` 不扫描系统，它 `readdir("/proc")`；`/proc` 的内容由**挂载时所在的 PID namespace** 决定。容器启动时 `runc` 先 `unshare(CLONE_NEWPID)`、`fork`，再在子进程里 `mount -t proc proc /proc`，于是 `/proc` 渲染的是新 ns 的编号表。宿主那 135 个照常跑，只是在容器的 `/proc` 里查不到。

```bash
# 1. 从宿主看：NSpid 一行给出两个编号
grep NSpid /proc/<宿主PID>/status
# NSpid:	3334084	1        ← 宿主 3334084，ns 内 1

# 2. 进入它的 ns 再看一遍
nsenter -t 3334084 -p -m ps -e
#     PID TTY          TIME CMD
#       1 ?        00:00:00 sleep     ← 同一个进程，编号变成 1
```

`NSpid` 第二个数字是「在最内层 pid ns 里的编号」，嵌套时有三列；`nsenter` 用 `setns(CLONE_NEWPID)` 加入后再 `fork`，所以看到 1 号就是那个 `sleep`。**PID 是 namespace 内的局部编号，不是全局唯一 ID。**
</details>

**Q2.** 你写了 `memory.max=64M`，容器里程序 `mmap` 了 200 MiB 匿名内存但**一个字节都没写**。会被 OOM kill 吗？换成 `malloc(200MB)` + `memset` 有什么不同？该看哪个文件？

<details><summary>答案</summary>

**不会。** `mmap` 200 MiB 匿名内存只记一个 VMA、改页表项，物理页一页没分配，RSS 不涨；本机实测 `memory.peak` 只有 3,268,608 B（3.1 MiB），`memory.events` 全 0。`malloc(200MB)` + `memset` 则触发**缺页中断**，内核逐页分配并清零，越过 64 MiB 后回收不动就 OOM kill——本机实测 `EXIT=137`，`peak` 精确等于 `67,108,864`，`oom_kill 1`。

该看 `memory.current`（含 page cache）、`memory.peak`、`memory.events`（`max` 是撞限次数，`oom_kill` 才是杀进程数）。**判据**：`memory.max` 限的是「已兑现的物理页 + page cache」，不是「承诺出去的虚拟地址」。
</details>

**Q3.** `pids.max=16`，容器里跑 `for i in $(seq 1 40); do sleep 3 & done`。它会打印什么？为什么不是 OOM killer 杀进程，而是 `fork` 直接失败？换成 `memory.max=16M` 挡同一个 fork bomb 会怎样？

<details><summary>答案</summary>

会打印 `sh: 0: Cannot fork`（本机实测），`pids.events` 记 `max 1`：`fork()` 返回 `EAGAIN`，进程根本没创建。pids 控制器在 `copy_process()` 路径上做**准入检查**，不是事后惩罚，所以没有进程被杀、也没有内存消耗。

换 `memory.max=16M` 则 `fork` 会成功，每个 `sleep` 占一点内存，RSS 越过 16 MiB 后回收不动才 OOM kill。问题是 fork bomb 常在内存涨到限额前先打满**宿主 PID 表**，那时整台机器都 `fork` 不了，连 `kill`、`ssh`、监控都失败。**所以防 fork bomb 必须用 `pids.max`。**
</details>

---

## 34.11 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 特权级、ring、VM vs 容器的取舍、隔离强度三角推导 | `base/15` |
| 一次 `syscall` 的全过程与成本模型 | `base/21` |
| `task_struct`、`fork`、COW、僵尸与收尸（1 号进程为何特殊） | `base/23` |
| 调度器、`cpu.max` 与 `cpu.weight` 的差异、`load average` | `base/25` |
| 页表、缺页、RSS/PSS 与 `memory.max` 的关系 | `base/26` |
| 内核漏洞、权限模型、seccomp 与逃逸攻防 | `base/33` |
| 沙箱四档选型、配额完整性、出口收敛性 | [llm/29](../llm/29-sandbox-isolation.md) |
| 全书总纲与本篇在 Part III 的位置 | [`0-summary.md`](0-summary.md) §0.4、§0.6.5 |
