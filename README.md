### 介绍
有些 Intel 主板 HDMI 在接 4K 显示器的时候，默认是以 YCbCr 420 方式显示的，会导致画质损失。如果强制设置为 RGB 或者 YCbCr 444，刷新率最高只能到 30Hz。似乎是被限制在了 HDMI 1.4 水平。

Linux 下在 sysfs 可以提取到 i915_vbt 文件，用开源工具 intel-gpu-tools 中的 intel_vbt_decode 对 VBT 进行解析，如果发现 HDMI 口数据速率被限制到了 297MHz，那么这种就是 BIOS 配置限制了 HDMI 的能力。

本仓库编写了一个脚本，可以对 i915_vbt 进行修改，解除速率限制并重新计算校验码，生成新的 vbt 文件。Linux 下把修改后的文件放到 /lib/firmware/i915/vbt.bin，然后在内核参数上指定 i915.vbt_firmware=i915/vbt.bin 即可得到解锁。注意通常 i915 驱动加载是在 ramdisk 阶段的，需要打包到 initcpio 中。

在 Windows 下就比较麻烦，需要修改 bios 才行，风险较大。大致的步骤如下：从官网下载 BIOS 文件或者 Dump BIOS 文件，用 uefitool 把 VBT 解包出来(注意 VBT 会有多份，需要找到有效的)，用脚本修改后再替换回 BIOS 文件中。将修改后的 BIOS 文件刷入，HDMI 速率限制就会得到解锁。

需要注意的是，解锁后最终显示是否正常也要看硬件能力是否能达标，不同的主板可能性能不一样。

### 脚本
仓库中的 patch_vbt.py 用于对 vbt 解除速率限制，使用方法
```
python patch_vbt.py vbt_file -o patched_file
```

### 测试过的主板
* 铭瑄 B660M 挑战者
