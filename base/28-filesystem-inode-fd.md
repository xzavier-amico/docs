# 28. 文件系统与「一切皆文件」：inode、dentry、fd 与 VFS

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 操作系统：Linux 视角 ｜ **编号**：28 / 56
> **本篇定位**：第三部分讲「内核提供哪些抽象」的第三篇。22 讲 ELF 怎么变成地址空间，23 讲进程怎么被 `fork` 出来，本篇讲**进程如何用一个整数（fd）访问「名字」背后的字节**，以及这个抽象在哪里漏。全篇只回答一个真问题：**文件不是内容，是一个 inode + 目录里的一条名字映射；「一切皆文件」是「一切都能用同一组 read/write/ioctl 去操作」的接口统一，不是「一切都是磁盘文件」。** 它接住上下两篇：往下是 `base/08`（一次 miss 为什么要等 100 μs 或 10 ms），往上是 `base/29`（`write` 成功为什么还不算落盘）。
> **读法**：§28.2 的四对象链是全篇骨架，建议照着自己机器 `ls -li` 一遍；§28.3 的 offset 实验解释「父子进程读同一个 fd 为什么会接着读」；§28.6 的删除实验是排障现场；§28.4 的支持矩阵用来纠正「一切皆文件 = 一切都能 seek」；赶时间读 §28.9–28.12。
> **不在本篇**：`write` 之后的页缓存、日志与 `fsync` → `base/29`；socket 收发路径与零拷贝 → `base/32`、`base/37`；VFS 之下的块层调度与 SSD 写放大 → `base/08`；WAL 与 B+ 树在存储引擎里的形态 → [comp/03](../comp/03-wal-bufferpool-crash-recovery.md)；对象存储为什么没有目录树与 inode → [comp/42](../comp/42-object-storage-and-cloud-disk.md)。

---

## 28.1 引子：`rm` 之后，磁盘为什么还满着

值班时最常见的一幕：磁盘告警，`df -h` 说根分区快满，`du -sh` 把每个目录加一遍却少了几百 MB，`find / -type f -size +100M` 只找到一堆正常的包。真正的占用者不在目录树里——它已经被 `rm` 掉了，但仍被某个进程的 fd 按着。

本文在 Debian 12 / 内核 6.12 / ext4（4 KiB 块）上原样复现。先分配 512 MiB，打开它，再删：

```bash
$ fallocate -l 512M bigtest/data.bin
$ ./holder bigtest/data.bin &          # 只 open + sleep
$ rm bigtest/data.bin
$ df -k /tmp | tail -1
/dev/vda1  20605384 10784208 8947580  55% /
$ du -k bigtest
4       bigtest
$ ls -l /proc/$PID/fd/3
lr-x------ ... /proc/3331734/fd/3 -> .../bigtest/data.bin (deleted)
$ kill $PID; df -k /tmp | tail -1
/dev/vda1  20605384 10260156 9471632  52% /
```

`df` 的已用从 10,783,968 变成 10,784,208（几乎没动），进程一退又掉到 10,260,156：`523,812 KiB ≈ 511.5 MiB` 正好是那个文件。而 `du` 只看到 4 KiB。三件看似矛盾的事同时成立：

| 你以为 | 其实 |
| --- | --- |
| `rm` 删除了文件 | `rm` 只删了**目录里的一条名字**；inode 与数据块由最后一个引用（fd 或 `mmap`）决定何时释放 |
| `df` 和 `du` 应该对得上 | `du` 遍历目录项、累加 `st_blocks`；`df` 问超级块还剩多少空闲块，还要算 inode 表、日志、保留块 |

本篇的推论链只有一条：**磁盘上只有 inode 表、目录数据块和位图；「文件」是它们在内存里被 VFS 拼出来的视图；fd 是这个视图的入口编号。** 沿着这条链，`rm` 不释放、`df`/`du` 不一致、fd 泄漏、inode 耗尽、跨文件系统 `rename` 失败，都不是五个 bug，而是同一个结构在五个方向的投影。

---

## 28.2 四个对象：inode、dentry、file、fd

### 磁盘上没有「树」，只有数组和目录数据块

把一个 ext4 卷摊开，只有四类东西：**超级块**（卷多大、多少 inode、空闲计数、日志在哪）、**inode 表**（按 inode 号索引的数组，每项 256 B，装着权限/属主/大小/时间/链接数/数据块指针）、**数据块**（文件内容、目录内容、extent 树分支）、**位图**（1 bit 表示一个块或一个 inode 用没用）。

**目录也是文件**，它的内容是一串变长记录，每条是「名字 → inode 号」。目录树是这些记录互相指出来的，磁盘上没有叫「树」的结构。

```
进程 task_struct
 └── files_struct
      └── fdtable:  [0]→/dev/tty  [1]→…  [2]→…  [3]──────────┐
                                                               │ 下标就是 fd
                                                               ▼
                                             struct file  (f_pos=12, f_flags=O_RDONLY, f_count=1)
                                                               │ f_path
                                                               ▼
                                             dentry ("a.txt") ──d_parent──▶ dentry ("d")
                                                               │ d_inode
                                                               ▼
                                             inode (i_ino=428763, i_nlink=2, i_size=12,
                                                    权限/时间, extent 树)
                                                               │ i_block → extent
                                                               ▼
                                             磁盘数据块 + 块位图
```

### 四个对象各是谁

| 对象 | 住在哪 | 关键内容 | 谁共享它 | 生命周期 |
| --- | --- | --- | --- | --- |
| **inode** | 磁盘 inode 表 + 内存 VFS 副本 | inode 号、模式、uid/gid、大小、时间、`i_nlink`、数据块/extent 指针 | 所有指向它的 dentry、所有 `file`、`mmap` | 从创建到最后一次 `iput`；无名字但有 fd 时仍活着 |
| **dentry** | 只在内存（dcache） | `(父 dentry, 名字)` → inode 的绑定；负 dentry 表示「查过，不存在」 | 所有路径解析、所有 `file` 的 `f_path` | 缓存对象，可回收；**不是**磁盘目录项 |
| **file** | 只在内存 | `f_pos`（offset）、`f_flags`、`f_mode`、`f_op`、`f_count`、`f_path` | `fork` 后父子共享、`dup` 后两个 fd 共享 | 从 `open` 到最后一个 fd 关闭 |
| **fd** | 进程的 `files_struct` | 一个整数下标，指向 `file` | 不共享（每个进程一张表） | 从 `open/dup` 到 `close`，或进程退出 |

三个必须说清的区别：**inode 没有名字**，名字在目录项里；同文件系统内改名或移动，inode 号不变、数据块不动，`cp` 才新建 inode。**dentry 不是目录项**：磁盘目录项是 `(name, ino)` 的持久记录，dentry 是 VFS 为「这个父目录下的这个名字」建的内存缓存，负 dentry 缓存「不存在」。**file 不是 inode**：同一 inode 可被 `open` 很多次，产生多个 `file`，各有自己的 offset 与 flags；`file` 是「这一次打开」，inode 是「文件本身」。

VFS 的核心结构可以缩到这几个字段（节选）：

```c
struct file {                       /* 打开文件描述，offset 住在这里 */
    struct path                 f_path;   /* { dentry, vfsmount } */
    struct inode               *f_inode;
    const struct file_operations *f_op;
    atomic_long_t               f_count;
    unsigned int                f_flags;  /* O_RDONLY / O_NONBLOCK / O_APPEND */
    fmode_t                     f_mode;
    loff_t                      f_pos;    /* ← fork/dup 共享的就是这个字段 */
    void                       *private_data;
};
```

**一句话回答唯一真问题**：文件的本体是 inode，名字是目录里的一条映射；`dentry` 是映射的内存缓存，`file` 是一次打开，`fd` 是这次打开在进程表里的下标。

---

## 28.3 fd 是下标，不是句柄

`open()` 返回的 `3` 不是指针，只是 `files_struct` 里 fd 数组的下标。`read(3, ...)` 的路径是：

```
fd = 3 → fdt->fd[3]（越界/空槽 → EBADF）→ struct file *f
       → f->f_op->read / read_iter → 用 f->f_pos 读写并推进
```

`fd` 按「最小可用下标」分配；`0/1/2` 只是 shell 与 libc 的约定，守护进程完全可以关掉 `0` 再 `open` 日志文件，让它拿到 fd 0。每个进程默认内联 64 个槽（`NR_OPEN_DEFAULT`），超过就分配更大的 `fdtable`，上限由 `RLIMIT_NOFILE` 卡住（本机 1,048,576；系统级 `file-max` 2,097,152）。

### `fork` / `dup` / `open` 的三种共享关系

| 操作 | fd 表 | `struct file` | offset | 结果 |
| --- | --- | --- | --- | --- |
| `open` 同一路径两次 | 两个槽 | **两个**对象 | 各自独立 | 两个 fd 各自从头读 |
| `fork` | 子进程复制父进程的 fd 表（指针） | **同一个**对象，`f_count++` | **共享** | 子进程读走的字节，父进程不会再读到 |
| `dup` / `dup2` | 新槽指向同一对象 | **同一个**对象，`f_count++` | **共享** | 关掉一个，另一个照常读写 |
| `open(..., O_APPEND)` | 新槽 | 新对象（同一 inode） | 每次写前原子移到末尾 | 多进程追加日志不互相覆盖 |

实测程序做四件事：先读 4 字节；`fork` 后子进程读 4 字节；父进程再读 4 字节；然后分别用二次 `open` 与 `dup` 再读。文件内容 `ABCDEFGHIJKLMNOPQRSTUVWXYZ`，输出：

```
parent-pre   read 4 bytes [ABCD]  offset after = 4
child        read 4 bytes [EFGH]  offset after = 8
parent-post  read 4 bytes [IJKL]  offset after = 12
separate     read 4 bytes [ABCD]  offset after = 4
dup          read 4 bytes [MNOP]  offset after = 16
fd and dup'd fd offsets: 16 / 16
```

`child` 从 **4** 开始读，不是从 0；`parent-post` 从 **8** 开始，说明父子共享同一个 `f_pos`。`separate` 重新 `open`，新 `file`，所以从 0 读 `ABCD`；`dup` 时 offset 已是 12，所以读 `MNOP`。**offset 属于 `struct file`，不属于 fd，也不属于进程。**

两个直接后果：

- **多线程共用一个 fd 读，数据会被切碎。** 两个线程各 `read(fd, buf, 4)`，内核不替它们配对。正确做法是 `pread(fd, buf, n, offset)`（offset 由调用者给，不改 `f_pos`）、每个线程自己 `open`、或外层加锁。
- **`O_NONBLOCK` 是文件状态，`FD_CLOEXEC` 是 fd 状态。** 前者在 `file->f_flags`，一个线程 `fcntl(F_SETFL, O_NONBLOCK)` 会影响同一 `file` 的所有持有者；后者只影响 `exec` 时这个槽关不关。Go/Java 的 netpoll 里「改一个连接的阻塞模式影响了别的协程」，根因就是它们共享 `file`。

`/proc/PID/fdinfo/N` 的 `pos` 就是 `f_pos`；两个进程的 `pos` 总是一起变，它们一定共享同一个 `file`（`fork` 或 `SCM_RIGHTS` 传 fd），而不是各自 `open`。

---

## 28.4 VFS 与「一切皆文件」：统一的是接口，不是语义

`read()` 不认识 ext4，也不认识 socket。它只做一次分派：

```
sys_read(fd, buf, count) → fdtable[fd] → struct file *f
                         → f->f_op->read(...) 或 read_iter(...)
```

`file_operations` 就是「这个对象支持哪些操作」的合同，槽位包括 `llseek/read/write/read_iter/write_iter/poll/unlocked_ioctl/mmap/fsync/splice_read` 等；哪个槽为空、返回什么，由具体对象决定。

**「一切皆文件」的准确含义是：一切对象都通过 `struct file` 暴露，用户用同一组 `open/read/write/ioctl/poll/close` 去操作；每个对象在 `f_op` 里实现其中一个子集。** 它不是「一切都是磁盘上的普通文件」，更不是「一切操作语义相同」。

### 六种 fd 的真实能力矩阵

本机用探针程序对同一组 fd 做 `read/write/lseek/mmap/poll`（`OK` 表示被接受，其余为真实 errno）：

| fd 背后 | read | write | lseek | mmap | poll |
| --- | --- | --- | --- | --- | --- |
| ext4 普通文件 | OK | OK | OK | OK | 总是 ready |
| pipe 读端 | OK（无数据则阻塞） | `EBADF`（读端不能写） | `ESPIPE` | `ENODEV` | 有数据才 ready |
| Unix socket | OK（流式，无消息边界） | OK | `ESPIPE` | `ENODEV` | 有数据才 ready |
| eventfd | 必须 8 字节 | 必须 8 字节 | 返回 0（`noop_llseek`） | `ENODEV` | 计数 > 0 才 ready |
| epoll fd | `EINVAL` | `EINVAL` | 返回 0（`noop_llseek`） | `ENODEV` | 有事件才 ready |
| `/dev/null` | OK（EOF） | OK（丢弃） | 返回 0 | `ENODEV` | 总是 ready |

几个「假统一」：

- **`lseek` 返回 0 不等于可 seek。** eventfd 和 epoll 的 `f_op` 用了 `noop_llseek`，永远返回 0，但对象没有「位置」概念；pipe/socket 直接返回 `ESPIPE`。对 eventfd `read` 1 字节是 `EINVAL`，必须读正好 8 字节。
- **`read` 的语义按类型分叉。** 普通文件读字节流并推进 offset；pipe 读队列里的一段（`PIPE_BUF` = 4096 以内写入原子）；socket 读流或报文；eventfd 读 8 字节计数器；epoll 根本不接受 `read`。`mmap` 也一样：普通文件和块设备可以，pipe/socket/eventfd/epoll 都是 `ENODEV`。
- **`ioctl` 是那个没有统一的洞。** `FIONREAD`、`TIOCGWINSZ`、`SIOCGIFADDR`、`EPOLL_CTL_ADD` 各自定义命令号与参数，VFS 只负责把 `(cmd, arg)` 转给驱动。

### 没有名字、没有磁盘块的 fd

pipe、socket、eventfd、epoll 在 `/proc/PID/fd` 里长这样：

```
2 -> pipe:[8498039]
5 -> socket:[8498857]
7 -> anon_inode:[eventfd]
8 -> anon_inode:[eventpoll]
```

它们没有路径、没有目录项、没有磁盘块，只有内核里的匿名 inode 与 `file`。`stat`、`close`、`poll` 都能工作——这正是「一切皆文件」真正统一的四件事：**一个整数句柄、一套引用计数生命周期、一组系统调用入口、一个能被 `poll/epoll` 等待的对象**。

`/proc` 和 `/sys` 是另一类：内容不是从磁盘读出来的。`/proc/meminfo` 的 `read` 走 `seq_file`，每次现算；`/sys/...` 的 `write` 直接调用驱动回调。它们把「文件」当成**内核对象的文本视图**。

所以准确地说：**Unix 的统一是「一切皆 fd」，不是「一切皆普通文件」。** Linux 只保证「你能用同一组 syscall 去试」，成不成立由 `f_op` 决定。

---

## 28.5 目录与硬链接：名字是目录里的一条记录

ext4 的目录数据块由一条条 `ext4_dir_entry_2` 组成：

```c
struct ext4_dir_entry_2 {
    __le32 inode;      /* inode 号；0 表示这条记录空闲 */
    __le16 rec_len;    /* 到下一条记录的字节偏移 */
    __u8   name_len;   /* 名字长度，≤ 255 */
    __u8   file_type;  /* 普通文件/目录/软链接... */
    char   name[];     /* 变长名字，不补 '\0' */
};
```

`ls` 用的是 `getdents64`，不是 `read`；对目录 `read()` 会得到 `EISDIR`。大目录用 `dir_index`（htree）把名字哈希到某个块，查找从 O(n) 降到近似 O(1)；但落到 VFS 都是「(父目录, 名字) → inode」，dcache 缓存的也是这个键。

硬链接就是「再写一条目录项，指向同一个 inode」：

```bash
$ printf 'hello inode\n' > a.txt
$ ln a.txt b.txt
$ ln -s a.txt c.txt
$ ls -li
428763 -rw-r--r-- 2 root root 12 ... a.txt
428763 -rw-r--r-- 2 root root 12 ... b.txt
428764 lrwxrwxrwx 1 root root  5 ... c.txt -> a.txt
```

两行 `428763` 是同一个 inode；`2` 是 `i_nlink`，表示「有两条目录项指向我」。软链接是另一个 inode（428764），大小 5 正好是目标字符串 `"a.txt"` 的长度——**软链接的内容就是一条路径**。

| 维度 | 硬链接（`ln`） | 软链接（`ln -s`） |
| --- | --- | --- |
| inode | 与目标**同一个** | **新的** inode，内容是目标路径 |
| `i_nlink` | +1 | 不变 |
| 跨文件系统 | 不允许（`EXDEV`） | 允许，存的是路径字符串 |
| 指向目录 | 不允许（`EPERM`，防止环） | 允许 |
| 目标删除后 | 只要还有一条名字，文件仍在 | 变成悬空链接（dangling） |
| `stat` 的 `st_size` | 文件内容大小 | 目标路径字符串长度 |

**目录的 `i_nlink` 有一条可推导的公式：`2 + 子目录数`。** `2` 来自「目录自己的 `.`」和「父目录里指向它的那条记录」；每多一个子目录，子目录里的 `..` 又多指回它一次。本机实测：`d2` 的 links 从 3 变 4（`mkdir sub2`），再回到 3（`rmdir sub2`）；`sub` 的 links=2（自己的 `.` + 父目录那条记录）。`mkdir` 让父目录 +1，`rmdir` 让它 −1。

硬链接数量上限在 ext4 上是 `EXT4_LINK_MAX = 65000`（`i_links_count` 是 16 位）；目录因 `dir_nlink` 可以超过，普通文件不行。硬链接不能指向目录，否则 `..` 的语义会被破坏、`fsck` 要处理环——内核直接 `EPERM`；`..` 是唯一例外，由内核在 `mkdir` 时维护。

**名字不是身份。** 同文件系统内 `mv`，inode 不变；跨文件系统 `rename(2)` 返回 `EXDEV`，`mv` 退化成「复制 + 删除」，inode 变了。判断「是不是同一个文件」要比较 `(st_dev, st_ino)`，不能比较路径：

```c
struct stat st;
fstat(fd, &st);          /* 对已打开的 fd 取，避免路径被换掉 */
printf("%lu:%lu\n", (unsigned long)st.st_dev, (unsigned long)st.st_ino);
```

安全代码的硬规则：**先 `open`，再 `fstat` 拿 `(dev, ino)`，后续检查都基于这个 fd**。路径可以在两次调用之间被替换（TOCTOU），inode 不会。

---

## 28.6 删除的真相：`unlink` 只减一个计数

`unlink(path)` 精确到四步：解析到父目录与最后一级名字；在父目录数据块里删掉那条 `(name, ino)`；`i_nlink--`；如果 `i_nlink == 0` **且** `i_count == 0`（没有 `file`、没有 `mmap`、没有其他内核引用），才释放 inode 与数据块。

第 4 步的「且」就是全部问题的来源。**`rm` 删的是名字；存储回收要等最后一个引用消失。**

### 一次完整的复现

| 时刻 | `df` 已用（1K 块） | `du bigtest` | `i_nlink` | `/proc/PID/fd/3` |
| --- | --- | --- | --- | --- |
| `fallocate` 后 | 10,783,968 | 524,296 | 1 | 不存在（还没 open） |
| `rm` 后、fd 还开着 | 10,784,208 | 4 | **0** | `... (deleted)` |
| 进程退出后 | 10,260,156 | 4 | — | 消失 |

`stat -L /proc/$PID/fd/3` 给出 `ino=429110 nlink=0 size=536870912 blocks=1048584`。`blocks` 以 512 B 计：`1,048,584 × 512 = 536,875,008 B ≈ 512 MiB`。**`df` 数块，`du` 数「目录项下面挂着的块」；文件没有名字，`du` 就找不到它，但块位图仍标着「已用」。**

### 怎么定位「谁按着这个已删除文件」

有 `lsof` 时一条 `lsof +L1`；本机没有，用 `/proc` 等价实现：

```bash
for p in /proc/[0-9]*; do
  for f in "$p"/fd/*; do
    t=$(readlink "$f" 2>/dev/null) || continue
    case "$t" in
      *"(deleted)"*)
        s=$(stat -Lc %s "$f" 2>/dev/null) &&
        printf '%10s  pid=%s  %s\n' "$s" "${p#/proc/}" "$t" ;;
    esac
  done
done | sort -rn | head
```

真实输出：

```
 268435456  pid=3331734  /tmp/opencode/fs28/reclaim/x.bin (deleted)
```

第一列是字节数，按大小倒序就能看出「谁吃了 10 GB」。本机用 512 MiB 复现；把 `512M` 换成 `10G` 就是线上场景。

### 处置：三条路

| 处置 | 命令 | 代价 |
| --- | --- | --- |
| 重启/优雅退出 | `kill -TERM $PID` | 服务中断；但最干净 |
| 让进程重开日志 | `kill -HUP $PID`（程序需实现 reopen） | 无中断，取决于应用 |
| 不重启，直接清空 | `truncate -s 0 /proc/$PID/fd/N` | 立即释放数据块；但 `f_pos` 不归零，下一次写会形成空洞（稀疏文件） |

第三条本机实测有效：`df` 从 10,544,984 降到 10,283,156，fd 仍指向 `(deleted)` 但内容没了。它适合「日志写疯了、先止血」；不适合「进程还要继续写这个文件」。**根治办法永远是让写入方重新 `open` 新文件**，这也是日志轮转 `copytruncate` 与 `rename + SIGHUP` 的分水岭：`rename` 派会让进程继续写「已删除的旧文件」，`copytruncate` 派 inode 不变、进程不用 reopen，但复制与截断之间有一个丢日志的窗口。

`O_TMPFILE` 是这条机制的正面用法：创建一个没有名字的 inode，写完再 `linkat` 给它名字，或者直接关闭让它消失；`mkstemp` + `unlink` 是可移植版本。**`mmap` 也持有 inode 引用**：`unlink` 一个被 `mmap` 的文件，磁盘同样不释放，直到 `munmap`。

---

## 28.7 文件系统结构：以 ext4 为例，inode 数量在 `mkfs` 时就定死了

ext4 把卷切成固定大小的**块组**，每组 32,768 个块（本机 4 KiB 块 → 128 MiB）。每组自带一套元数据，便于局部性和损坏隔离：

```
块组 0                          块组 1                        ...
┌──────────────────────┐        ┌──────────────────────┐
│ 超级块（备份）        │        │ 备份超级块            │
│ 组描述符表 GDT        │        │ GDT                   │
│ 块位图（1 bit/块）    │        │ 块位图                │
│ inode 位图（1 bit/个）│        │ inode 位图            │
│ inode 表（8000×256B） │        │ inode 表              │
│ 数据块 ...            │        │ 数据块 ...            │
└──────────────────────┘        └──────────────────────┘
```

本机 `dumpe2fs -h /dev/vda1` 的关键字段：

```
Inode count:          1280000
Block count:          5242619
Reserved block count: 214303
Overhead clusters:    91273
Block size:           4096
Blocks per group:     32768
Inodes per group:     8000
Inode blocks per group: 500
Inode size:           256
Journal inode:        8
Total journal size:   32M
Total journal blocks: 8192
First inode:          11
```

把这些数字互相验算一遍（数字的出生证明）：

| 字段 | 数值 | 推导 |
| --- | --- | --- |
| 卷大小 | 5,242,619 块 | `× 4096 B = 21,473,687,552 B ≈ 20.0 GiB` |
| 块组数 | 160 | `5,242,619 ÷ 32,768 ≈ 160` |
| inode 总数 | 1,280,000 | `160 × 8000 = 1,280,000` |
| 每组 inode 表 | 500 块 = 2 MiB | `8000 × 256 B = 2,048,000 B = 500 × 4096 B` |
| 全卷 inode 表 | 80,000 块 ≈ 312.5 MiB | `160 × 500 × 4096 = 327,680,000 B` |
| 日志 | 8,192 块 = 32 MiB | `8192 × 4096 = 33,554,432 B` |
| 元数据开销 | 91,273 块 ≈ 356.5 MiB | inode 表 312.5 + 日志 32 + 位图/GDT/备份约 12 |
| 保留块 | 214,303 块 ≈ 837 MiB | 留给 root，普通用户 `df` 的 avail 不含它 |

`Overhead clusters: 91273` 和上面的加法能对上：**20 GiB 的卷，光 inode 表就吃掉 312.5 MiB，约 1.5%**。这就是 `df` 的 used 永远比 `du` 的累加多一点的原因之一——`du` 不会去数 inode 表。块位图每组正好 1 个块，inode 位图不到 1 个块；`flex_bg` 把 16 个组的元数据聚在一起，所以 `dumpe2fs` 会显示「Group 1 的 inode table 在 bg #0 + 783」——物理上并不在自己的组里。

### inode 数量为什么改不了

`mke2fs` 建卷时按 `inode_ratio` 一次性分配 inode 表。本机 `/etc/mke2fs.conf`：

```
[defaults]
    blocksize = 4096
    inode_size = 256
    inode_ratio = 16384     # 每 16 KiB 空间配 1 个 inode

[fs_types]
    small = { blocksize = 1024  inode_ratio = 4096 }
    big   = { inode_ratio = 32768 }
    huge  = { inode_ratio = 65536 }
```

20 GiB 的理论上限是 `20 × 1024 × 1024 ÷ 16 ≈ 1,310,720`，实际 1,280,000（扣掉元数据开销），量级完全吻合。

| `inode_ratio` | 每个 inode 管多少数据 | 20 GiB 卷理论上限 | 适合 |
| --- | --- | --- | --- |
| 4,096 | 4 KiB | ≈ 5.2 M | 小文件极多（邮件队列、容器层、`node_modules`） |
| 16,384（默认） | 16 KiB | ≈ 1.31 M | 通用 |
| 65,536 | 64 KiB | ≈ 327 K | 大文件（视频、备份、数据库） |

**这是建卷时的一次性选择，建完不能给已有块组补 inode。** ext4 可以 `resize2fs` 扩容，新块组按同一比例带新 inode，但老组不变；XFS、Btrfs 动态分配，没有这个硬上限。

### 把 inode 用完：一个 64 个 inode 的文件系统

```bash
$ truncate -s 16M tiny.img
$ mke2fs -q -t ext4 -N 64 -F tiny.img
$ mount -o loop tiny.img mnt
$ df -h mnt | tail -1
/dev/loop0       15M   14K   14M   1% /tmp/opencode/fs28/mnt
$ df -i mnt | tail -1
/dev/loop0         64    64     0  100% /tmp/opencode/fs28/mnt
$ touch mnt/one_more
touch: cannot touch 'mnt/one_more': No space left on device
```

**块只用了 1%，inode 用了 100%。** `mkfs` 自己用掉 11 个（inode 1–10 保留：坏块、root、ACL、日志 inode 8、resize inode 7 等；inode 11 是 `lost+found`），剩 53 个给用户；54 个目录项 = 53 个文件 + `lost+found`，再 `touch` 就 `ENOSPC`。

排障上极常见的误判：**`ENOSPC` 既可能是块没了，也可能是 inode 没了，`errno` 是同一个。** 只看 `df -h` 会以为「还有 14M 可用，怎么可能写不进去」；必须 `df -i`。大量小文件（Docker 镜像层、`node_modules`、邮件 spool、Kafka 索引段）都是 inode 先于块耗尽。

---

## 28.8 路径解析的成本：`open("/a/b/c")` 是 N 次查找

`open("/a/b/c")` 不是「拿字符串查一张表」，而是逐级下降：选起点（绝对路径从根、相对路径从 `cwd`，`openat` 从 `dirfd`）；对每个中间分量检查搜索权限（`x`）、在 dcache 按 `(父 dentry, 名字)` 查，未命中就读目录数据块，找不到就 `ENOENT` 并留下负 dentry，遇到软链接展开（上限 40 层，超过 `ELOOP`），遇到挂载点切到该挂载的根 dentry；最后一级按打开模式与权限位检查，分配 `struct file`，在 fd 表找最小空槽。

所以成本 = **分量数 × 每级 dcache 查找** + **miss 次数 × 目录块 IO** + 一次 `file` 分配。路径越深，不是字符串更长，而是**查找次数线性增加**。

本机用 6 个分量与 25 个分量的路径各 `open+close` 20 万次，取 5 轮最小值：

```
shallow  6 components: min   8479 ns/open
deep    25 components: min  12134 ns/open
delta per component:          183 ns
```

第二轮是 `9202 / 12366 / 158 ns`。绝对值包含容器 seccomp 与审计开销，**不能当裸金属 syscall 成本**；能用的结论是差值：**全在 dcache 里时，每多一级约 160–180 ns**。冷缓存是另一个世界：按 `base/08` 的量级，SSD 4 KiB 随机读约 100 μs，HDD 寻道约 5–10 ms；一个 10 级冷路径最坏是 1 ms（SSD）到 100 ms（HDD）。这就是「刚启动第一次 `find` 特别慢」和「`ls` 快、`find` 慢」的物理原因：`find` 对每个子目录重复 open → getdents → 对每个名字 `stat`，解析次数是文件数的量级。

dcache 的状态可以直接读：

```
$ cat /proc/sys/fs/dentry-state
9806    6708    45    0    1580    0
```

前两个数是「已分配 dentry 数」与「LRU 上可回收的未使用数」，最后一个是负 dentry 数（1,580）。负 dentry 让「反复检查不存在的路径」不必每次下磁盘，代价是内存；扫描大量不存在的名字会把 dcache 撑大，虽可回收，但会挤压其他页缓存。

### `openat`、`O_PATH` 与不跟随路径

**`openat(dirfd, "b/c", ...)`** 的价值有两层：性能上前缀只解析一次；安全上起点被钉住，不会因为中间目录被 `rename` 而漂移。但它仍允许 `..` 和绝对路径软链接逃逸。Linux 5.6 的 `openat2` 用 `RESOLVE_BENEATH` / `RESOLVE_IN_ROOT` / `RESOLVE_NO_SYMLINKS` 堵口子。本机实测（`d` 下有一个指向 `/etc/passwd` 的 `escape`）：

```
BENEATH d/a.txt              -> fd=4  errno=0  ok
BENEATH d/../d/a.txt         -> fd=-1 errno=18 Invalid cross-device link
BENEATH d/escape             -> fd=-1 errno=18 Invalid cross-device link
no flag d/escape             -> fd=5  errno=0  ok
```

`..` 与「指向外部的软链接」都被拒绝，`errno` 是 `EXDEV` 而不是 `EACCES`——故意的：**不告诉你外面那个路径存不存在**。不带 `RESOLVE_BENEATH` 的 `openat` 则老实跟到 `/etc/passwd`。

**`O_PATH`** 是「只要路径，不要读写」的 fd：

```
O_PATH fd=3 fstat=0 size=12
read  -> -1 errno=9 Bad file descriptor
lseek -> -1 errno=9 Bad file descriptor
reopen via /proc/self/fd/3 -> fd=4 read=3 [hel]
```

它可以 `fstat`、当 `dirfd`、通过 `/proc/self/fd/N` 重新打开成真正的 fd；但不能 `read/write/lseek/mmap`。它不要求目标文件本身有读权限，只要求路径上各级目录有搜索权限，适合「安全地持有一个位置」。容器运行时、`systemd`、`tar` 解包大量使用它来避免 TOCTOU。

### `rename` 的原子性边界

```
rename(/tmp/... , /dev/shm/...) -> -1 errno=18 Invalid cross-device link
rename(same fs)                 ->  0 errno=0  Success
```

`rename(2)` 只在**同一个文件系统内**保证「对并发观察者原子」：要么看到旧名字，要么看到新名字，不会看到半个。跨文件系统它直接 `EXDEV`，`mv` 退化成「读旧 → 写新 → 删旧」，**没有原子性**，中途崩溃会留下不完整的新文件。所以「临时文件 + rename 做原子替换」必须保证临时文件与目标在**同一个挂载点**下；`/tmp` 与 `/var` 分属不同文件系统时，`rename` 会失败。还要加限定：`rename` 的原子性是**对进程观察者**的，崩溃后新名字是否可见取决于目录项的持久化（`fsync` 目录），这是 `base/29` 的账。

---

## 28.9 实测：五组能自己复现的数字

环境：Debian 12，内核 6.12，ext4，4 KiB 块，root；没有 `lsof`，全部用 `/proc` 替代；`dumpe2fs`、`mke2fs` 可用。

| # | 实验 | 命令 | 真实观测 | 证明了 |
| --- | --- | --- | --- | --- |
| ① | 硬链接同 inode | `ls -li` / `stat` | `a.txt`、`b.txt` 都是 `428763`；软链接 `428764`、`size=5` | 名字是目录项，inode 才是文件 |
| ② | link count | `stat -c '%i %h'` | 两个名字 `links=2`；`rm` 一个后 `links=1`；全删且 fd 开着时 `links=0` | `unlink` 只减计数 |
| ③ | 打开后删除 | `rm` + `df -k` + `/proc/PID/fd` | `df` 不减；`du` 只剩 4 KiB；fd 指向 `(deleted)` | 存储延迟回收 |
| ④ | `df -i` | `df -i /tmp` | 1,280,000 总数、203,035 已用、1,076,965 空闲（16%） | inode 是独立资源 |
| ⑤ | fd 上限 | `ulimit -n` + 泄漏程序 | 默认 1,048,576；降到 64 后打开 61 个即 `EMFILE` | fd 是有限资源 |

### ① 与 ②：同 inode 的两个名字，链接计数

```
$ stat -c 'inode=%i links=%h size=%s name=%n type=%F' a.txt b.txt c.txt
inode=428763 links=2 size=12 name=a.txt type=regular file
inode=428763 links=2 size=12 name=b.txt type=regular file
inode=428764 links=1 size=5  name=c.txt type=symbolic link
```

`rm b.txt` 后 `a.txt` 的 `links` 变 1；两个名字都删掉、但 fd 还开着时，`/proc/PID/fd/N` 上的 `nlink=0`（§28.6 的表）。

### ③：删除后空间不回来

```
$ df -k /tmp | tail -1
/dev/vda1  20605384 10784208 8947580  55% /
$ du -k bigtest
4       bigtest
$ stat -Lc 'ino=%i nlink=%h size=%s blocks=%b' /proc/$PID/fd/3
ino=429110 nlink=0 size=536870912 blocks=1048584
$ kill $PID; df -k /tmp | tail -1
/dev/vda1  20605384 10260156 9471632  52% /
```

### ④：`df -i` 与 `df -h` 是两本账

```
$ df -i /tmp | tail -1
/dev/vda1      1280000 203035 1076965   16% /
$ df -h /tmp | tail -1
/dev/vda1        20G  8.1G   11G  43% /
$ df -i mnt | tail -1          # 16 MiB、-N 64 的镜像
/dev/loop0         64    64     0  100% /tmp/opencode/fs28/mnt
$ df -h mnt | tail -1
/dev/loop0       15M   14K   14M   1% /tmp/opencode/fs28/mnt
```

### ⑤：fd 上限与泄漏

```
$ ulimit -n
1048576
```

把软限制降到 64，跑一个「只 `open` 不 `close`」的程序（系统级 `file-max` 2,097,152，进程级 `nr_open` 1,048,576，三个上限里先撞到哪个取决于哪个最小）：

```
opened 61 fds before failure: errno=24 Too many open files
fd count in /proc/3297016/fd: 64
```

`0/1/2` 占 3 个，`61 + 3 = 64`，正好等于 `RLIMIT_NOFILE`；第 62 次 `open` 失败，`errno=24`（`EMFILE`）。系统级 `file-max` 2,097,152，`nr_open` 1,048,576 是 `RLIMIT_NOFILE` 的上限：**先撞到哪个，取决于哪个最小**。

---

## 28.10 三列账：文件系统承诺了什么、收了多少、从哪开始崩

| 维度 | 承诺了什么 | 为此付了什么 | 从哪里开始失效 |
| --- | --- | --- | --- |
| **统一接口** | 一切对象都能 `open/read/write/poll/close`，用 fd 一个整数寻址 | 每个对象实现一张 `f_op`；语义按类型分叉；`ioctl` 退化成无类型逃生口 | 对 pipe/socket `lseek`、对 epoll `read`、对 socket `mmap` 全部失败；「一切皆文件」被误读成「一切都能 seek」 |
| **路径命名** | 人类可读的名字、目录树、硬链接、原子 `rename` | 每次 `open` 逐级解析；每级可能 dcache miss 到磁盘；`rename` 只在同 fs 原子 | 深路径冷启动慢；跨 fs `rename` 失败；路径可被替换（TOCTOU）；同名多路径让「按路径做权限」出错 |
| **inode 元数据** | 权限、属主、时间、大小、链接数、块映射集中在一处 | 每个 inode 256 B，建卷时一次性分配；inode 表占 1.5% 空间；每次 `stat` 是元数据访问 | inode 先用完（`df -i` 100%，块还剩很多）；海量小文件把 inode 表撑爆 |
| **fd 表** | 进程用最小整数访问任意对象；`fork` 免费继承；`poll` 统一等待 | 每个 fd 一个 `file` + 表槽 + 引用；受 `RLIMIT_NOFILE` 限制；offset/flags 共享带来并发坑 | fd 泄漏 → `EMFILE`；多线程共享 offset 读串数据；`O_NONBLOCK` 改一处影响全部持有者 |
| **页缓存** | 重复读不碰磁盘；`read` 命中内存；`mmap` 共享页 | 内存被文件页占用（可回收但会挤压）；`write` 成功不等于落盘 | `df`/`du` 与真实 IO 脱节；`write` 后崩溃丢数据；脏页回写造成延迟毛刺 |
| **删除与回收** | `unlink` 立即让名字消失；空间最终回收 | 回收延迟到最后一次 `close/munmap`；引用计数分散在 fd、mmap、dentry | `rm` 后 `df` 不减；日志轮转把磁盘写满；`O_TMPFILE` 忘了关也会泄漏 |
| **持久化布局** | 崩溃后元数据一致（日志）；目录不会指向半个 inode | 日志写放大；`fsync` 要等设备；跨层缓存各有一层谎 | 掉电丢最近写；`fsync` 返回后仍可能丢（磁盘缓存）；跨 fs 复制中途崩溃留下半个文件 |

一句话收束：**文件系统买到的是「名字稳定的字节容器 + 统一接口」，付出的是「元数据 IO、路径解析、有限的 fd/inode、延迟回收」。** 五个崩点不是五个缺陷，是这笔账的五个出口。

---

## 28.11 踩坑清单：现象 → 根因 → 定位 → 修 → 防复发

| 现象 | 根因（指回本篇） | 定位命令 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| `df` 满了，`du` 差很多 | 已删除但仍被 fd/`mmap` 持有（§28.6）；或 inode 表/日志/保留块（§28.7） | `lsof +L1`；没有就用 §28.6 的 `/proc/*/fd` 循环；`df -i`；`dumpe2fs -h` | 重启/`SIGHUP` 让进程重开；急用时 `truncate -s 0 /proc/PID/fd/N` | 日志轮转选 `copytruncate` 或实现 reopen；监控 `df` 时同时看 `df -i` |
| 服务突然 `Too many open files` | fd 泄漏（§28.3）：异常路径/循环里 `open` 没 `close` | `ls /proc/PID/fd \| wc -l` 持续上涨；`cat /proc/PID/limits` | 所有路径都 `close`（C 用 `goto cleanup`，Go 用 `defer`，Java 用 try-with-resources） | 压测时对比 fd 曲线；设 `LimitNOFILE` 留余量；评审专查早退路径 |
| `open` 成功但读不到内容，或读到旧内容 | `O_TRUNC` 漏了 / offset 被共享（§28.3） | `strace -e openat,read,lseek`；`/proc/PID/fdinfo/N` 看 `pos` | 要覆盖写就 `O_TRUNC`；并发读用 `pread` 或每线程独立 `open` | 封装 `open` 默认带 `O_CLOEXEC`；禁止多线程共用一个读 fd |
| `rename` 报 `EXDEV`，或 `mv` 后目标不完整 | 跨文件系统，`rename` 不原子（§28.8） | `stat -c '%d %n' src dst` 比 `st_dev`；`df` 看挂载点 | 临时文件必须建在目标目录同层；跨 fs 先在本 fs 内完成「写临时 → fsync → rename」 | 确保临时目录与数据目录同一挂载；不要用 `/tmp` 做原子替换暂存 |
| 还有空间却 `No space left on device` | inode 耗尽（§28.7） | `df -i` 对照 `df -h`；`find /path -xdev -type f \| wc -l` | 删小文件/归档；重建卷时降低 `inode_ratio` | 建卷按平均文件大小选 `inode_ratio`；监控 `IUse%` |
| 扫描目录时内存涨、dcache 很大 | 负 dentry/目录缓存膨胀（§28.8） | `cat /proc/sys/fs/dentry-state`；`slabtop -o \| head` | 停止无意义的全盘扫描；必要时 `echo 2 > /proc/sys/vm/drop_caches` | 避免对不可控路径重复 `stat`；用 `openat` 限定根 |
| 容器里读到宿主机文件 / 路径逃逸 | `openat` 跟了 `..` 或绝对软链接（§28.8） | 审查是否用 `openat2` + `RESOLVE_BENEATH`；检查软链接 | 改用 `openat2` 的 `RESOLVE_BENEATH/IN_ROOT`；对最终 fd 做 `fstat` 校验 `(dev, ino)` | 所有对外路径解析走统一封装；禁止「先检查字符串再 open」 |
| 进程继承了不该继承的 fd | `fork/exec` 时没设 `O_CLOEXEC`（§28.3） | `ls -l /proc/PID/fd` 看是否有父进程的 socket/日志 | `open(..., O_CLOEXEC)`；`fcntl(fd, F_SETFD, FD_CLOEXEC)` | 默认 `O_CLOEXEC`，只在确实要传的 fd 上显式清除 |

三句话：**空间对不上**先分「块 / inode / 已删除未释放」三本账；**fd 报错**先数 `/proc/PID/fd` 并找没关的早退路径；**路径怪事**先把「路径」换成「fd + (dev, ino)」再检查。

---

## 28.12 一页速记

```
【一句话】文件的本体是 inode，名字是目录里的一条 (name → ino)；fd 是进程 fd 表的下标。

【四对象】
  inode   元数据 + 数据块指针；无名字；i_nlink 是硬链接数；256 B（ext4）
  dentry  内存缓存 (父目录, 名字) → inode；负 dentry 缓存「不存在」；不在磁盘上
  file    一次打开：f_pos / f_flags / f_op / f_count；offset 住在这里
  fd      整数下标；fork 复制指针共享 file；dup 也共享；open 新建 file

【三条硬事实】
  ① rm 只减 i_nlink；i_nlink==0 且 i_count==0 才释放 → df 不减、du 看不见
  ② inode 数在 mkfs 时定死（inode_ratio）；df -h 有空间也可能 ENOSPC → 看 df -i
  ③ lseek 不是所有 fd 都有：pipe/socket = ESPIPE；eventfd/epoll 返回 0 但不代表能 seek

【三个数字】
  20 GiB 卷 = 5,242,619 × 4 KiB；160 组 × 8000 inode = 1,280,000 个 inode
  inode 表 = 1,280,000 × 256 B = 312.5 MiB（约占 1.5%）
  fd 软限 1,048,576；系统 file-max 2,097,152；PIPE_BUF 4096

【一张判断表】
  df 满 du 不满   → 已删除但 fd/mmap 还开着（/proc/*/fd 找 deleted）
  df -h 有余量却 ENOSPC → inode 耗尽（df -i）
  读同一个 fd 数据乱 → offset 共享（pread / 独立 open）
  rename EXDEV    → 跨文件系统，不原子（临时文件放目标目录）
```

---

## 28.13 自检 3 题

**Q1.** 父进程 `open` 一个文件并读了 4 字节，然后 `fork`。子进程读 4 字节，父进程再读 4 字节。两次读到的内容分别从第几字节开始？如果父进程在 `fork` 前 `dup` 了 fd，答案会变吗？如果父进程在 `fork` 后重新 `open` 同一路径呢？

<details><summary>答案</summary>

`fork` 复制的是 fd 表里的指针，父子指向**同一个 `struct file`**，所以 `f_pos` 共享。父进程先读到 offset 4，子进程从 4 开始读（第 5–8 字节），offset 变 8；父进程接着从 8 读（第 9–12 字节），offset 变 12。本机实测就是 `child [EFGH]`、`parent-post [IJKL]`。

`fork` 前 `dup` **不改变**结论：`dup` 的新 fd 也指向同一个 `file`，只是 fd 表多一个槽；父子仍共享 offset。

`fork` 后重新 `open` 同一路径，**会变**：新 `open` 创建新的 `struct file`，`f_pos` 从 0 开始，与父进程原来的 fd 完全独立。本机实测 `separate [ABCD] offset 4`。判断共享关系看「是不是同一个 `file`」，不是看 fd 号。
</details>

**Q2.** 线上 `df -h` 显示根分区 100%，`du -sh /*` 加起来只有 60%；`df -i` 也很空闲。列出至少三种可能原因和定位命令。其中「进程持有已删除文件」这一种，如何在不重启进程的前提下把空间拿回来？代价是什么？

<details><summary>答案</summary>

三种常见原因：

1. **已删除但仍被打开**：`lsof +L1`，没有就用 §28.6 的 `/proc/*/fd` 循环找 `(deleted)`；`du` 看不到没有名字的 inode。
2. **元数据与保留块**：inode 表、日志、位图、root 保留块都计入 `df` 的 used，但 `du` 不数；看 `dumpe2fs -h` 的 `Overhead clusters` 与 `Reserved block count`。
3. **挂载边界 / 硬链接 / 稀疏文件**：`du` 默认不跨挂载点，硬链接分开算会重复，稀疏文件的 `st_size` 远大于 `st_blocks`；用 `findmnt`、`stat -c '%h %s %b'`、`du -x` 对照。

应急回收：`truncate -s 0 /proc/$PID/fd/N`。它直接截断 inode，数据块立即释放，`df` 马上下降（本机从 10,544,984 降到 10,283,156）。**代价**：`f_pos` 不会归零，下一次 `write` 会在旧偏移处写，文件变成带洞的稀疏文件。所以它只适合止血，根治要让写入方重新 `open`（`SIGHUP`/重启/logrotate 配置）。
</details>

**Q3.** 为什么 `pipe` 的 `lseek` 返回 `ESPIPE`、`mmap` 返回 `ENODEV`，而 `eventfd` 的 `lseek` 返回 0？既然 eventfd 的 `lseek` 成功了，是不是说明它可以 seek？「一切皆文件」到底统一了什么、没统一什么？

<details><summary>答案</summary>

`pipe` 是内核里的环形缓冲区，没有「第 N 个字节」这个概念；它的 `file_operations` 没有实现 `llseek`，落到默认路径时返回 `ESPIPE`（不可 seek 的流），也没有可映射的后端页，所以 `mmap` 返回 `ENODEV`。

`eventfd` 的 `f_op` 显式用了 `noop_llseek`，这个函数**永远返回 0**，不做任何位置计算。返回 0 只表示「这个调用没报错」，不表示对象有位置：对 eventfd `read` 1 字节是 `EINVAL`，必须读正好 8 字节。同理，`epoll` 的 `lseek` 也返回 0，但 `read/write` 都是 `EINVAL`——它只接受 `epoll_ctl` 和 `poll`。

「一切皆文件」统一的是：**一个 fd 整数、一套引用计数生命周期、一组系统调用入口、一个能被 `epoll` 等待的对象**。它没有统一：可 seek 性、消息边界、读写原子性、阻塞语义、`ioctl` 的命令空间。正确说法是「一切皆 fd / 一切皆 `file_operations` 的一个子集」，不是「一切都是普通磁盘文件」。
</details>

---

## 28.14 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 器件物理：为什么一次目录块 miss 是 100 μs（SSD）到 10 ms（HDD） | → `base/08` |
| 虚拟内存与 `mmap`：文件页如何进页表、RSS 为什么不降 | → `base/27` |
| 落盘与崩溃：`write` 成功之后的页缓存、日志、`fsync` 三层谎 | → `base/29` |
| 零拷贝四实现：`read/write`、`mmap`、`sendfile`、`splice` 的拷贝账 | → `base/37` |
| WAL、B+ 树与崩溃恢复：存储引擎如何复用文件系统这层抽象 | → [comp/03](../comp/03-wal-bufferpool-crash-recovery.md) |
| 对象存储与云盘：没有目录树、没有 inode 的世界如何寻址 | → [comp/42](../comp/42-object-storage-and-cloud-disk.md) |
| 全书总纲：四条主线与 56 篇清单 | [0-summary](0-summary.md) |
