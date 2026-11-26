# 部署

本文说明如何把 MUS Booking 装到一台 Ubuntu 24.04 服务器上。服务以一个 systemd 服务运行，只监听本机的 8000 端口，由服务器上已有的 Caddy 以 `https://booking.sichenwang.com` 转发进来。Caddy 负责 HTTPS 证书，本服务不碰 80 和 443 端口。

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

先在 Cloudflare 上为域名添加 A 记录，指向服务器的 IP，并设为“仅 DNS”（灰云）。经 Cloudflare 代理从国内访问偏慢，而服务器的 IP 早已公开，代理带来的好处有限。然后把 `deploy/Caddyfile.snippet` 里的站点块加进现有的 Caddyfile 并重新加载，Caddy 会自动为域名申请证书。Caddy 运行在 Docker 里并使用主机网络时，命令如下。

```sh
docker exec new-api-caddy caddy reload --config /etc/caddy/Caddyfile
```

这台服务器上的 Caddyfile 是以单个文件挂载进容器的。单文件挂载跟随的是文件本身而不是路径，像 `sed -i` 那样先写新文件再改名的保存方式会让容器继续读到旧内容，因此要原地写入，写完先用 `docker exec new-api-caddy caddy validate --config /etc/caddy/Caddyfile` 校验，再重新加载。上线时改动前的原文件备份为 `/opt/new-api/Caddyfile.bak-20261125-before-mus-booking`，换域名前的备份在 `/root/domain-migration-20261126-091403/`。

Caddy 从本机转发，默认会带上 `X-Forwarded-For` 和 `X-Forwarded-Proto`，服务只信任来自本机的这两个头：登录限流按真实的客户端地址计算，HTTPS 下的 Cookie 带上 Secure。表单提交要求 Origin 与请求的 Host 一致，所以反向代理不要改写 Host。

服务也可以挂在某个路径下，例如 `https://服务器地址/booking/`：Caddy 用 `handle_path /booking/*` 去掉前缀后转发，服务启动时加 `--root-path /booking` 把前缀补回到页面上的每个链接里，两者必须一致。上线当天就是这样挂在 IP 地址下的，次日改为子域名，旧地址在 IP 的站点块里 308 跳转到新域名的同一路径，写法见 `deploy/Caddyfile.snippet` 的注释。换地址后，浏览器里的登录状态不会跟过去，成员要在新地址重新登录一次，手机主屏幕上的图标也要重新添加；服务器上的数据不受影响。

## 开始使用

```sh
cd /opt/mus-booking
sudo -u mus-booking sh -c 'set -a; . /etc/mus-booking/env; .venv/bin/mus-booking admin 名字'
```

这条命令设置管理账号，会要求输入两次密码，至少 8 位；数据库里只存密码的散列。之后在登录页的学号一栏填这个名字（不分大小写），再填密码，就进入管理页。学号都是数字，所以名字不能是纯数字。

管理账号只管理，不对应任何成员；在管理员的浏览器里登录别的成员，新会话不带管理权限。管理员自己也要订琴房的话，像其他成员一样用邀请码绑定，用学号登录即可。忘记密码时重新运行上面的命令，所有浏览器上的管理权限随之收回。

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
