import warnings

from keras.utils import pad_sequences

warnings.filterwarnings("ignore")
import itertools
import pandas as pd
import numpy as np
from tqdm import tqdm
from collections import namedtuple

import tensorflow as tf
from keras.layers import *
from keras.models import *

from sklearn.model_selection import train_test_split
from sklearn.preprocessing import MinMaxScaler, LabelEncoder

from utils import SparseFeat, DenseFeat, VarLenSparseFeat


# 构建输入层
# 将输入的数据转换成字典的形式，定义输入层的时候让输入层的name和字典中特征的key一致，就可以使得输入的数据和对应的Input层对应
def build_input_layers(feature_columns):
    input_layer_dict = {}

    for fc in feature_columns:
        if isinstance(fc, SparseFeat):
            input_layer_dict[fc.name] = Input(shape=(1,), name=fc.name)
        elif isinstance(fc, DenseFeat):
            input_layer_dict[fc.name] = Input(shape=(fc.dimension,), name=fc.name)
        elif isinstance(fc, VarLenSparseFeat):
            input_layer_dict[fc.name] = Input(shape=(fc.maxlen,), name=fc.name)

    return input_layer_dict


# 构建embedding层
def build_embedding_layers(feature_columns, input_layer_dict):
    embedding_layer_dict = {}

    for fc in feature_columns:
        if isinstance(fc, SparseFeat):
            embedding_layer_dict[fc.name] = Embedding(fc.vocabulary_size, fc.embedding_dim, name='emb_' + fc.name)
        elif isinstance(fc, VarLenSparseFeat):
            embedding_layer_dict[fc.name] = Embedding(fc.vocabulary_size + 1, fc.embedding_dim, name='emb_' + fc.name,
                                                      mask_zero=True)

    return embedding_layer_dict


def embedding_lookup(feature_columns, input_layer_dict, embedding_layer_dict):
    embedding_list = []

    for fc in feature_columns:
        _input = input_layer_dict[fc]
        _embed = embedding_layer_dict[fc]
        embed = _embed(_input)
        embedding_list.append(embed)

    return embedding_list


class Dice(Layer):
    def __init__(self):
        super(Dice, self).__init__()
        self.bn = BatchNormalization(center=False, scale=False)

    def build(self, input_shape):
        self.alpha = self.add_weight(shape=(input_shape[-1],), dtype=tf.float32, name='alpha')

    def call(self, x):
        x_normed = self.bn(x)
        x_p = tf.sigmoid(x_normed)

        return self.alpha * (1.0 - x_p) * x + x_p * x


class LocalActivationUnit(Layer):

    def __init__(self, hidden_units=(256, 128, 64), activation='prelu'):
        super(LocalActivationUnit, self).__init__()
        self.hidden_units = hidden_units
        self.linear = Dense(1)
        self.dnn = [Dense(unit, activation=PReLU() if activation == 'prelu' else Dice()) for unit in hidden_units]

    def call(self, inputs):
        # query: B x 1 x emb_dim  keys: B x len x emb_dim
        query, keys = inputs

        # 获取序列长度
        keys_len = keys.get_shape()[1]

        queries = tf.tile(query, multiples=[1, keys_len, 1])  # (None, len, emb_dim)

        # 将特征进行拼接
        att_input = tf.concat([queries, keys, queries - keys, queries * keys], axis=-1)  # B x len x 4*emb_dim

        # 将原始向量与外积结果拼接后输入到一个dnn中
        att_out = att_input
        for fc in self.dnn:
            att_out = fc(att_out)  # B x len x att_out

        att_out = self.linear(att_out)  # B x len x 1
        att_out = tf.squeeze(att_out, -1)  # B x len

        return att_out


class AttentionPoolingLayer(Layer):
    def __init__(self, att_hidden_units=(256, 128, 64)):
        super(AttentionPoolingLayer, self).__init__()
        self.att_hidden_units = att_hidden_units
        self.local_att = LocalActivationUnit(self.att_hidden_units)

    def call(self, inputs):
        # keys: B x len x emb_dim, queries: B x 1 x emb_dim
        queries, keys = inputs

        # 获取行为序列embedding的mask矩阵，将Embedding矩阵中的非零元素设置成True，
        key_masks = tf.not_equal(keys[:, :, 0], 0)  # B x len
        # key_masks = keys._keras_mask # tf的有些版本不能使用这个属性，2.1是可以的，2.4好像不行

        # 获取行为序列中每个商品对应的注意力权重
        attention_score = self.local_att([queries, keys])  # B x len

        # 去除最后一个维度，方便后续理解与计算
        # outputs = attention_score
        # 创建一个padding的tensor, 目的是为了标记出行为序列embedding中无效的位置
        paddings = tf.zeros_like(attention_score)  # B x len

        # outputs 表示的是padding之后的attention_score
        outputs = tf.where(key_masks, attention_score, paddings)  # B x len

        # 将注意力分数与序列对应位置加权求和，这一步可以在
        outputs = tf.expand_dims(outputs, axis=1)  # B x 1 x len

        # keys : B x len x emb_dim
        outputs = tf.matmul(outputs, keys)  # B x 1 x dim
        outputs = tf.squeeze(outputs, axis=1)

        return outputs


def get_dnn_logits(dnn_input, hidden_units=(200, 80), activation='prelu'):
    dnns = [Dense(unit, activation=PReLU() if activation == 'prelu' else Dice()) for unit in hidden_units]

    dnn_out = dnn_input
    for dnn in dnns:
        dnn_out = dnn(dnn_out)

    # 获取logits
    dnn_logits = Dense(1, activation='sigmoid')(dnn_out)

    return dnn_logits


# 输入层拼接成列表
def concat_input_list(input_list):
    feature_nums = len(input_list)
    if feature_nums > 1:
        return Concatenate(axis=1)(input_list)
    elif feature_nums == 1:
        return input_list[0]
    else:
        return None


# 将所有的sparse特征embedding拼接
def concat_embedding_list(feature_columns, input_layer_dict, embedding_layer_dict, flatten=False):
    embedding_list = []
    for fc in feature_columns:
        _input = input_layer_dict[fc.name]  # 获取输入层
        _embed = embedding_layer_dict[fc.name]  # B x 1 x dim  获取对应的embedding层
        embed = _embed(_input)  # B x dim  将input层输入到embedding层中

        # 是否需要flatten, 如果embedding列表最终是直接输入到Dense层中，需要进行Flatten，否则不需要
        if flatten:
            embed = Flatten()(embed)

        embedding_list.append(embed)

    return embedding_list


def DIN(feature_columns, behavior_feature_list, behavior_seq_feature_list):
    # 构建Input层
    input_layer_dict = build_input_layers(feature_columns)

    # 将Input层转化成列表的形式作为model的输入
    input_layers = list(input_layer_dict.values())

    # 筛选出特征中的sparse特征和dense特征，方便单独处理
    sparse_feature_columns = list(filter(lambda x: isinstance(x, SparseFeat), feature_columns))
    dense_feature_columns = list(filter(lambda x: isinstance(x, DenseFeat), feature_columns))

    # 获取dense
    dnn_dense_input = []
    for fc in dense_feature_columns:
        dnn_dense_input.append(input_layer_dict[fc.name])

    # 将所有的dense特征拼接
    dnn_dense_input = concat_input_list(dnn_dense_input)

    # 构建embedding字典
    embedding_layer_dict = build_embedding_layers(feature_columns, input_layer_dict)

    # 因为这里最终需要将embedding拼接后直接输入到全连接层(Dense)中, 所以需要Flatten
    dnn_sparse_embed_input = concat_embedding_list(sparse_feature_columns, input_layer_dict, embedding_layer_dict,
                                                   flatten=True)

    # 将所有sparse特征的embedding进行拼接
    dnn_sparse_input = concat_input_list(dnn_sparse_embed_input)

    # 获取当前的行为特征(movie)的embedding，这里有可能有多个行为产生了行为序列，所以需要使用列表将其放在一起
    query_embed_list = embedding_lookup(behavior_feature_list, input_layer_dict, embedding_layer_dict)

    # 获取行为序列(movie_id序列, hist_movie_id) 对应的embedding，这里有可能有多个行为产生了行为序列，所以需要使用列表将其放在一起
    keys_embed_list = embedding_lookup(behavior_seq_feature_list, input_layer_dict, embedding_layer_dict)

    # 使用注意力机制将历史movie_id序列进行池化
    dnn_seq_input_list = []
    for i in range(len(keys_embed_list)):
        seq_emb = AttentionPoolingLayer()([query_embed_list[i], keys_embed_list[i]])
        dnn_seq_input_list.append(seq_emb)

    # 将多个行为序列attention poolint 之后的embedding进行拼接
    dnn_seq_input = concat_input_list(dnn_seq_input_list)

    # 将dense特征，sparse特征，及通过注意力加权的序列特征拼接
    dnn_input = Concatenate(axis=1)([dnn_dense_input, dnn_sparse_input, dnn_seq_input])

    # 获取最终dnn的logits
    dnn_logits = get_dnn_logits(dnn_input, activation='prelu')

    model = Model(input_layers, dnn_logits)
    return model


if __name__ == "__main__":
    # 读取数据
    behaviors_data = pd.read_csv("./data/MINDsmall_train/behaviors.tsv", sep="\t",
                                 names=["impression_id", "user_id", "time", "history", "impressions"])
    news_data = pd.read_csv("./data/MINDsmall_train/news.tsv", sep="\t",
                            names=["news_id", "category", "sub_category", "title", "abstract", "url", "title_entities",
                                   "abstract_entities"])

    padded_history_sequences = pad_sequences(
        [list(map(str, l.split())) if isinstance(l, str) else [] for l in behaviors_data["history"]], maxlen=50,
        padding='post', truncating='post', dtype=object, value='<PAD>')
    padded_impression_sequences = pad_sequences(
        [[n.split('-')[0] for n in s.split()] if isinstance(s, str) else [] for s in
         behaviors_data["impressions"]], maxlen=1, padding='post', truncating='post',
        dtype=object, value='<PAD>')

    user_news_features = []  # 存储每个用户的新闻特征
    for history in behaviors_data['impressions']:
        news_ids = [history.split()[0].split('-')[0]]
        user_news_feature = []  # 每个用户的新闻特征
        for news_id in news_ids:
            news_row = news_data[news_data['news_id'] == news_id]
            # 提取需要的新闻特征
            # news_feature = [news_row['category'].values[0], news_row['sub_category'].values[0],
            #                 news_row['title'].values[0], news_row['abstract'].values[0]]
            news_feature = [news_row['category'].values[0]]
            user_news_feature.append(news_feature)
        user_news_features.append(user_news_feature)

    X_train = {
        "user_id": np.array(behaviors_data["user_id"]),
        "time": np.array(behaviors_data["time"]),
        "history": padded_history_sequences,
        "imp_news_id": padded_impression_sequences,
        "user_news_features": np.array(user_news_features)
    }

    y_train = np.array(
        [int(n.split('-')[1]) if isinstance(s, str) else 0 for s in behaviors_data["impressions"] for n in
         s.split()[:1]])

    print("Number of samples in X_train:", len(X_train))
    print("Sample content in X_train:", X_train)
    print("y_train", y_train)

    # 假设将"news_id"作为稀疏特征中的id，"category"和"sub_category"作为类别特征
    feature_columns = [SparseFeat('user_id', vocabulary_size=len(behaviors_data['user_id'].unique()), embedding_dim=8),
                       # SparseFeat('time', vocabulary_size=len(behaviors_data['time'].unique()) + 1, embedding_dim=8),
                       VarLenSparseFeat('history', vocabulary_size=len(behaviors_data['history'].unique()),
                                        embedding_dim=8, maxlen=50),
                       SparseFeat('imp_news_id', vocabulary_size=len(behaviors_data['impressions'].unique()),
                                  embedding_dim=8),
                       SparseFeat('user_news_features', vocabulary_size=len(news_data['category'].unique()),
                                  embedding_dim=8),
                       DenseFeat('time', 1)
                       ]

    # 打印特征列的定义
    for feat in feature_columns:
        if isinstance(feat, (SparseFeat, VarLenSparseFeat)):
            print(f"{feat.name}: embedding_dim={feat.embedding_dim}, vocabulary_size={feat.vocabulary_size}")
        elif isinstance(feat, DenseFeat):
            print(f"{feat.name}: dense_feature_dim={feat.dimension}")

    # 检查X_train的特征形状
    for feature_name, feature_value in X_train.items():
        print(f"{feature_name}: {feature_value.shape}")

    behavior_feature_list = ['imp_news_id']
    behavior_seq_feature_list = ['history']

    model = DIN(feature_columns, behavior_feature_list, behavior_seq_feature_list)

    model.compile(optimizer='adam', loss='binary_crossentropy', metrics=['accuracy'])

    # 将数据划分为训练集和验证集
    # X_train, X_val, y_train, y_val = train_test_split(X_train, y_train, test_size=0.2, random_state=42)

    model.fit(X_train, y_train, batch_size=64, epochs=5, validation_split=0.2)
