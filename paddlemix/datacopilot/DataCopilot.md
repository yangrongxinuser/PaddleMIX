# DataCopilot 使用教程

## 一、简介

**DataCopilot** 是 **PaddleMIX** 提供的多模态数据处理工具箱，旨在帮助开发者高效地进行数据预处理、增强和转换等操作。通过 **DataCopilot**，你可以以低代码量的方式实现数据的基本操作，从而加速模型训练和推理的过程。

## 二、安装与导入

首先，确保你已经安装了 **PaddleMIX**。如果尚未安装，请参考 **PaddleMIX** 的官方文档进行安装。

安装完成后，你可以通过以下方式导入 **DataCopilot**：

```python
from paddlemix.datacopilot.core import MMDataset, SCHEMA
import paddlemix.datacopilot.ops as ops
```

## 三、基本操作

### 1. 加载数据

使用 `MMDataset.from_json` 方法从 JSON 文件中加载数据：

```python
dataset = MMDataset.from_json('path/to/your/dataset.json')
```

### 2. 查看数据

使用 info 和 head 方法查看数据集的基本信息和前几个样本：

```python
dataset.info()
dataset.head()
```

### 3. 数据切片

支持对数据集进行切片操作，返回一个新的 MMDataset 对象：

```python
subset = dataset[:100]  # 获取前100个样本
```

### 4. 数据增强

使用 map 方法对数据集中的样本进行增强操作：

```python
def augment_data(item):
    # 定义你的数据增强逻辑
    pass

augmented_dataset = dataset.map(augment_data, max_workers=8, progress=True)
```

### 5. 数据过滤

使用 filter 方法根据条件过滤数据集中的样本：

```python
def is_valid_sample(item):
    # 定义你的过滤条件
    return True or False

filtered_dataset = dataset.filter(is_valid_sample).nonempty()  # 返回过滤后的非空数据集
```

### 6. 导出数据

使用 export_json 方法将处理后的数据集导出为 JSON 文件：

```python
augmented_dataset.export_json('path/to/your/output_dataset.json')
```

## 四、高级操作

### 1. 自定义 Schema

通过定义 SCHEMA 来指定数据集的字段和类型：

```python
schema = SCHEMA(
    image={'type': 'image', 'required': True},
    text={'type': 'str', 'required': True},
    label={'type': 'int', 'required': False}
)
```
使用自定义 schema 加载数据

```python
custom_dataset = MMDataset.from_json('path/to/your/dataset.json', schema=schema)
```


### 2. 批量处理

使用 batch 方法将数据集中的样本按批次处理，适用于需要批量操作的情况：

```python
batch_size = 32
batched_dataset = dataset.batch(batch_size)

for batch in batched_dataset:
    # 对每个批次进行处理
    pass
```

### 3. 数据采样

使用 shuffle 方法打乱数据集，或使用 sample 方法随机抽取样本：

```python
shuffled_dataset = dataset.shuffle()
sampled_dataset = dataset.sample(10)  # 随机抽取10个样本
```

## 五、总结

**DataCopilot** 是 **PaddleMIX** 提供的一个强大且灵活的多模态数据处理工具箱。
通过掌握其基本操作和高级功能，你可以高效地处理、增强和转换多模态数据，为后续的模型训练和推理提供有力支持。




