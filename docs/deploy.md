# 部署

本文说明如何把 MUS Booking 装到一台 Ubuntu 24.04 服务器上。服务以一个 systemd 服务运行，只监听本机的 8000 端口，由服务器上已有的 Caddy 在 `https://服务器地址/booking/` 下转发进来。Caddy 负责 HTTPS 证书，本服务不碰 80 和 443 端口。

下面的命令都以 root 身份执行，仓库放在 `/opt/mus-booking`，数据放在 `/var/lib/mus-booking`，密钥和配置放在 `/etc/mus-booking`。

## 安装

服务器需要能访问学校的系统。先装好 uv，再用它在仓库里建立虚拟环境；虚拟环境使用系统自带的 Python 3.12，这样以服务账号运行时不依赖 root 的家目录。

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
useradd --system --home /opt/mus-booking --shell /usr/sbin/nologin mus-booking
git clone https://github.com/sichen-wang/MUS-Booking.git /opt/mus-booking
cd /opt/mus-booking
UV_PYTHON_PREFERENCE=only-system ~/.local/bin/uv sync --frozen --no-dev
.venv/bin/mus-booking check
```

`check` 不提交任何账号信息，只依次访问统一认证、网关和场地系统。三项都通过再继续。

## 配置与密钥

配置文件只有三项：数据目录、密钥文件和对外地址。密钥用来加密同学的学校密码，必须只有服务账号能读，并且与数据库分开备份。

```sh
install -d -o mus-booking -g mus-booking -m 700 /etc/mus-booking /var/lib/mus-booking
install -o root -g mus-booking -m 640 deploy/env.example /etc/mus-booking/env
nano /etc/mus-booking/env
sudo -u mus-booking sh -c 'set -a; . /etc/mus-booking/env; /opt/mus-booking/.venv/bin/mus-booking init'
```

`init` 生成密钥文件并建好数据库；密钥文件已经存在时不会覆盖。systemd 单元把整个文件系统设为只读，只放开 `/var/lib/mus-booking`，所以数据目录不要改到别处；确实要改时，同时修改单元里的 `ReadWritePaths` 和 `StateDirectory`。

## 启动服务

```sh
cp deploy/mus-booking.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mus-booking
journalctl -u mus-booking -f
```

日志里出现 `Uvicorn running on http://127.0.0.1:8000` 即表示服务已经起来；它前面的 `Application startup complete` 出现时端口还没有绑定，不能作为依据。没有密钥文件时服务会拒绝启动，在日志里说明原因，并且不再自动重启。

## 接入 Caddy

把 `deploy/Caddyfile.snippet` 的内容加进现有 Caddyfile 中服务器地址的那个站点块，然后重新加载。Caddy 运行在 Docker 里并使用主机网络时，命令如下。

```sh
docker exec new-api-caddy caddy reload --config /etc/caddy/Caddyfile
```

这台服务器上的 Caddyfile 是以单个文件挂载进容器的。单文件挂载跟随的是文件本身而不是路径，像 `sed -i` 那样先写新文件再改名的保存方式会让容器继续读到旧内容，因此要原地写入，写完先用 `docker exec new-api-caddy caddy validate --config /etc/caddy/Caddyfile` 校验，再重新加载。上线时改动前的原文件备份为 `/opt/new-api/Caddyfile.bak-20261125-before-mus-booking`。

`handle_path` 会去掉地址里的 `/booking`，服务启动时的 `--root-path /booking` 再把它补回到页面上的每个链接里，两者必须一致。Caddy 默认会带上 `X-Forwarded-For` 和 `X-Forwarded-Proto`，服务只信任来自本机的这两个头。

## 开始使用

```sh
cd /opt/mus-booking
sudo -u mus-booking sh -c 'set -a; . /etc/mus-booking/env; .venv/bin/mus-booking admin 名字'
```

这条命令设置管理账号，会要求输入两次密码，至少 8 位；数据库里只存密码的散列。之后在登录页的学号一栏填这个名字（不分大小写），再填密码，就进入管理页。学号都是数字，所以名字不能是纯数字。

用管理账号登录之后，在同一个浏览器里绑定或登录自己的学校账号，两者就对应起来：以后在任何设备上用管理账号登录一次，就同时回到自己的首页和管理页。忘记密码时重新运行这条命令，对应关系随之解除，下次按同样的方式再对应。

邀请码在管理页上生成，可以直接复制带邀请码的链接发给同学；在服务器上也可以用 `mus-booking invite` 生成。

## 更新与备份

```sh
cd /opt/mus-booking
git pull
UV_PYTHON_PREFERENCE=only-system ~/.local/bin/uv sync --frozen --no-dev
systemctl restart mus-booking
```

避开 20:50 到 21:05 之间重启。重启后服务会自动补交已经开放而还没提交的预定。

需要备份的是数据库和 `/etc/mus-booking/secret.key`。数据库开着 WAL，最近的写入可能还在旁边的 `-wal` 文件里，直接复制 `.db` 文件会漏掉它们，要用 SQLite 自己的备份命令：

```sh
apt install sqlite3
sqlite3 /var/lib/mus-booking/mus-booking.db ".backup '/root/mus-booking-backup.db'"
```

数据库和密钥应分开存放：只拿到数据库的人看不到任何密码，丢了密钥则保存的密码都无法解开，同学需要重新登录一次。

## 时间

服务一律按中国时间计算开抢时刻，与服务器的时区设置无关，但要求系统时钟经 NTP 同步。用 `timedatectl` 确认 `System clock synchronized: yes`。每晚的记录里会存下学校服务器与本机的时间差，可以在数据库的 `runs` 表里查看。
