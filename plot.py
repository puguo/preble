import re
import matplotlib.pyplot as plt

# 读取日志文件
log_file = "output.txt"  # 修改为你的日志文件路径
with open(log_file, "r") as f:
    lines = f.readlines()

# 初始化数据存储
timestamps = []
gpu_counts = []
total_utilizations = []
avg_utilizations = []

# 解析日志
for i, line in enumerate(lines):
    match = re.search(r"Utilization record: active_gpu: (\[.*?\]), utilizations: (\[.*?\])", line)
    if match:
        active_gpus = eval(match.group(1))  # 解析 GPU 列表
        utilizations = eval(match.group(2))  # 解析利用率列表
        
        num_gpus = len(active_gpus)  # GPU 数量
        total_utilization = sum(utilizations)  # 总利用率
        avg_utilization = total_utilization / num_gpus if num_gpus > 0 else 0  # 平均利用率

        timestamps.append(i)  # 这里用行号作为时间
        gpu_counts.append(num_gpus)
        total_utilizations.append(total_utilization)
        avg_utilizations.append(avg_utilization)

# 画第一张图（GPU 数量随时间变化）
plt.figure(figsize=(10, 5))
plt.plot(timestamps, gpu_counts, marker="o", linestyle="-", label="Number of GPUs")
plt.xlabel("Time (log line index)")
plt.ylabel("Number of GPUs")
plt.title("Number of GPUs Over Time")
plt.legend()
plt.grid(True)
plt.savefig("gpu_count_over_time.png")
plt.close()

# 画第二张图（总利用率和平均利用率）
plt.figure(figsize=(10, 5))
plt.plot(timestamps, total_utilizations, marker="o", linestyle="-", label="Total Utilization")
plt.plot(timestamps, avg_utilizations, marker="s", linestyle="--", label="Average Utilization")
plt.xlabel("Time (log line index)")
plt.ylabel("Utilization")
plt.title("Total and Average Utilization Over Time")
plt.legend()
plt.grid(True)
plt.savefig("utilization_over_time.png")
plt.close()

print("Plots saved as 'gpu_count_over_time.png' and 'utilization_over_time.png'")
