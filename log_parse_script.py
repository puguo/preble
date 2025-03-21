import re
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from datetime import datetime

def parse_log_file(filename):
    # 存储提取的数据
    metrics_data = []
    utilization_data = []
    
    with open(filename, 'r') as f:
        for line in f:
            if "GPU" in line and "Utilization:" in line:
                # 解析GPU指标行
                timestamp = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line).group(1)
                gpu_id = re.search(r'GPU (\d+):', line).group(1)
                util = float(re.search(r'Utilization: ([\d.]+)%', line).group(1))
                ttft = float(re.search(r'Avg TTFT: ([\d.]+)', line).group(1))
                tpot = float(re.search(r'Avg TPOT: ([\d.]+)', line).group(1))
                queue_len = float(re.search(r'Avg Waiting Queue Length: ([\d.]+)', line).group(1))
                
                metrics_data.append({
                    'timestamp': datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S'),
                    'gpu_id': int(gpu_id),
                    'ttft': ttft,
                    'tpot': tpot,
                    'queue_len': queue_len,
                    'utilization': util
                })
                
            elif "Utilization record:" in line:
                # 解析利用率记录行
                timestamp = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line).group(1)
                gpu_ids = re.search(r'active_gpu: \[([\d, ]+)\]', line).group(1).split(',')
                utils = re.search(r'utilizations: \[([\d, ]+)\]', line).group(1).split(',')
                waiting_keys = re.search(r'waiting_keys: \[([\d, ]+)\]', line).group(1).split(',')
                queue_nums = re.search(r'waiting_queue_nums: \[([\d, ]+)\]', line).group(1).split(',')
                
                for i in range(len(gpu_ids)):
                    utilization_data.append({
                        'timestamp': datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S'),
                        'gpu_id': int(gpu_ids[i]),
                        'utilization': int(utils[i]),
                   #     'queue_size': int(queue_nums[i])
                    })
    
    return pd.DataFrame(metrics_data), pd.DataFrame(utilization_data)

def calculate_averages(metrics_df):
    # 计算非零值的平均值
    avg_metrics = {
        'TTFT': metrics_df[metrics_df['ttft'] > 0]['ttft'].mean(),
        'TPOT': metrics_df[metrics_df['tpot'] > 0]['tpot'].mean(),
        'Queue Length': metrics_df[metrics_df['queue_len'] > 0]['queue_len'].mean()
    }
    # 计算P99值
    p99_metrics = {
        'TTFT': metrics_df[metrics_df['ttft'] > 0]['ttft'].quantile(1-0.99),
        'TPOT': metrics_df[metrics_df['tpot'] > 0]['tpot'].quantile(1-0.99),
        'Queue Length': metrics_df['queue_len'].quantile(1-0.99)
    }
    
    median_metrics = {
        'TTFT': metrics_df['ttft'].median(),
        'TPOT': metrics_df['tpot'].median(),
        'Queue Length': metrics_df['queue_len'].median()
    }
    
    return avg_metrics, p99_metrics
    return avg_metrics

def plot_metrics(metrics_df, util_df):
    # 设置图表风格
    plt.style.use('seaborn')
    
    # 创建子图
    fig, axes = plt.subplots(4, 1, figsize=(15, 20))
    
    # 1. TTFT随时间变化
    sns.lineplot(data=metrics_df, x='timestamp', y='ttft', hue='gpu_id', ax=axes[0])
    axes[0].set_title('Time to First Token (TTFT) over Time')
    axes[0].set_ylabel('TTFT (seconds)')
    
    # 2. TPOT随时间变化
    sns.lineplot(data=metrics_df, x='timestamp', y='tpot', hue='gpu_id', ax=axes[1])
    axes[1].set_title('Time Per Output Token (TPOT) over Time')
    axes[1].set_ylabel('TPOT (seconds)')
    
    # 3. 等待队列长度随时间变化
    sns.lineplot(data=metrics_df, x='timestamp', y='queue_len', hue='gpu_id', ax=axes[2])
    axes[2].set_title('Waiting Queue Length over Time')
    axes[2].set_ylabel('Queue Length')
    '''
    # 4. GPU利用率随时间变化
    sns.lineplot(data=util_df, x='timestamp', y='utilization', hue='gpu_id', ax=axes[3])
    axes[3].set_title('GPU Utilization over Time')
    axes[3].set_ylabel('Utilization (%)')
    '''
    # 调整布局
    plt.tight_layout()
    plt.savefig('gpu_metrics_analysis_waitingqueue.png')
    plt.close()

def main():
    # 解析日志文件
    metrics_df, util_df = parse_log_file('gpu_metrics_log_4card_waiting_queue.txt')
    
    # 计算平均值
    averages, p99s = calculate_averages(metrics_df)
    print("\nAverage Metrics (non-zero values):")
    for metric, value in averages.items():
        print(f"Average {metric}: {value:.4f}")
    print("\nP99 Metrics:")
    for metric, value in p99s.items():
        print(f"P99 {metric}: {value:.4f}")
    # 绘制图表
    plot_metrics(metrics_df, util_df)
    print("\nPlots have been saved to 'gpu_metrics_analysis_waitingqueue.png'")

if __name__ == "__main__":
    main()