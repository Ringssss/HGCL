import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.nn import GCNConv, HypergraphConv
from torch_geometric.data import Data
from torch_geometric.utils import dropout_edge
import random
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score
import pickle
import numpy as np
import torch.optim as optim
import torch.optim.lr_scheduler as lr_scheduler

NUM_EPOCHS = 500
LR = 0.001
TEMPERATURE = 0.7
MASK_RATIO = 0.3
EDGE_DROP_RATIO = 0.2
T_DIFFUSION = 20
BETA_START = 0.0001
BETA_END = 0.02
GAMMA = 0.8
HIDDEN_CHANNELS = 512
EMB_DIM = 256
KNN_K = 15
PATIENCE = 30
WEIGHT_DECAY = 1e-5
BATCH_SIZE_KNN = 512
DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


random.seed(42)
torch.manual_seed(42)
np.random.seed(42)

def build_knn_hypergraph(x, k=KNN_K):
    
    num_nodes = x.size(0)
    indices = []

    for i in range(0, num_nodes, BATCH_SIZE_KNN):
        batch = x[i:i+BATCH_SIZE_KNN]
        # 计算当前batch与所有节点的余弦相似度
        sim_batch = F.cosine_similarity(
            batch.unsqueeze(1), 
            x.unsqueeze(0), 
            dim=-1
        )

        topk = torch.topk(sim_batch, k=k+1, dim=1)
        batch_indices = topk.indices[:, 1:]  # 排除自身
        indices.append(batch_indices)
    
    indices = torch.cat(indices, dim=0)

    node_list = []
    hedge_list = []
    for i in range(num_nodes):
        neighbors = indices[i].tolist()
        for j in neighbors:
            node_list.append(i)
            hedge_list.append(j)
    
    return torch.tensor([node_list, hedge_list], dtype=torch.long)

def contrastive_loss(z1, z2, temperature=TEMPERATURE):

    z1 = F.normalize(z1, dim=1)
    z2 = F.normalize(z2, dim=1)
    batch_size = z1.size(0)
    
    # Positive pairs: same node in different views
    positives = torch.sum(z1 * z2, dim=1) / temperature
    
    # Negative pairs: all pairs except diagonal
    negatives = torch.mm(z1, z2.t()) / temperature
    
    # Mask out self-comparisons
    mask = torch.eye(batch_size, dtype=torch.bool, device=z1.device)
    negatives = negatives.masked_fill(mask, -9e15)
    
    # Compute loss
    numerator = positives
    denominator = torch.logsumexp(negatives, dim=1)
    loss = -torch.mean(numerator - denominator)
    return loss


class HGCLEncoder(nn.Module):
    def __init__(self, in_channels, hidden_channels, out_channels):
        super().__init__()
        self.gcn1 = GCNConv(in_channels, hidden_channels)
        self.gcn2 = GCNConv(hidden_channels, hidden_channels)
        self.hgc1 = HypergraphConv(in_channels, hidden_channels)
        self.hgc2 = HypergraphConv(hidden_channels, hidden_channels)
        self.project = nn.Sequential(
            nn.Linear(hidden_channels, out_channels),
            nn.ReLU(),
            nn.Linear(out_channels, out_channels)
        )
        self.ln1 = nn.LayerNorm(hidden_channels)
        self.ln2 = nn.LayerNorm(hidden_channels)

    def forward(self, x, edge_index, hyperedge_index=None):
        h1 = F.relu(self.ln1(self.gcn1(x, edge_index)))
        h1 = F.relu(self.ln1(self.gcn2(h1, edge_index)))
        if hyperedge_index is not None:
            h2 = F.relu(self.ln2(self.hgc1(x, hyperedge_index)))
            h2 = F.relu(self.ln2(self.hgc2(h2, hyperedge_index)))
            h = (h1 + h2) / 2
        else:
            h = h1
            
        return self.project(h)


class DiffusionDenoiser(nn.Module):
    def __init__(self, channels):
        super().__init__()
        self.time_mlp = nn.Sequential(
            nn.Linear(1, channels),
            nn.ReLU(),
            nn.Linear(channels, channels)
        )
        self.conv1 = GCNConv(channels, channels)
        self.conv2 = GCNConv(channels, channels)
        self.ln = nn.LayerNorm(channels)

    def forward(self, h_noisy, edge_index, t):
        t = t.view(-1, 1).float()
        t_emb = self.time_mlp(t)
        t_emb = t_emb.expand(h_noisy.size(0), -1)
        
        h = h_noisy + t_emb
        h = F.relu(self.ln(self.conv1(h, edge_index)))
        return self.conv2(h, edge_index)


class H_GCL(nn.Module):
    def __init__(self, in_channels, hidden_channels, out_channels):
        super().__init__()
        self.encoder_x = HGCLEncoder(in_channels, hidden_channels, out_channels)
        self.encoder_y = HGCLEncoder(in_channels, hidden_channels, out_channels)
        self.denoiser = DiffusionDenoiser(out_channels)


        self.beta = torch.linspace(BETA_START, BETA_END, T_DIFFUSION).to(DEVICE)
        self.alpha = 1 - self.beta
        self.alpha_cum = torch.cumprod(self.alpha, dim=0)

    def augment(self, x, edge_index):
        # Feature masking
        mask = torch.rand_like(x) > MASK_RATIO
        x_bar = x * mask
        
        edge_index_bar, _ = dropout_edge(edge_index, p=EDGE_DROP_RATIO, force_undirected=True)
        return x_bar, edge_index_bar

    def forward(self, x, edge_index, hyperedge_index):
        # Augmentations
        x_bar, edge_index_bar = self.augment(x, edge_index)
        

        h_x = self.encoder_x(x_bar, edge_index_bar)  # Graph view
        h_y = self.encoder_y(x_bar, edge_index_bar, hyperedge_index)  # Hypergraph view
        

        loss_c = contrastive_loss(h_x, h_y)
        

        t = random.randint(0, T_DIFFUSION - 1)
        sqrt_alpha_cum = torch.sqrt(self.alpha_cum[t])
        sqrt_one_minus = torch.sqrt(1 - self.alpha_cum[t])
        noise = torch.randn_like(h_x)
        h_x_noisy = sqrt_alpha_cum * h_x + sqrt_one_minus * noise
        
        t_tensor = torch.tensor([t / T_DIFFUSION], device=DEVICE)
        h_x_hat = self.denoiser(h_x_noisy, edge_index_bar, t_tensor)
        
        loss_g = F.mse_loss(h_x_hat, h_x)
        
        # Total loss
        loss = GAMMA * loss_c + (1 - GAMMA) * loss_g
        return loss, h_x.detach()


def load_local_cora(local_dir='/home/zhujianian/others/planetoid-master/data/'):
    def load_pickle(filename):
        with open(local_dir + filename, 'rb') as f:
            return pickle.load(f, encoding='latin1')

    tx = load_pickle('ind.cora.tx')
    ty = load_pickle('ind.cora.ty')
    allx = load_pickle('ind.cora.allx')
    ally = load_pickle('ind.cora.ally')
    graph = load_pickle('ind.cora.graph')
    with open(local_dir + 'ind.cora.test.index', 'r') as f:
        test_idx = [int(i) for i in f]


    idx = np.sort(test_idx)
    x = np.vstack((allx.todense(), tx.todense()))
    x[test_idx] = x[idx]  # Fix order

    y = np.concatenate((ally, ty), axis=0)
    y[test_idx] = y[idx]

    # Build edge_index
    row = []
    col = []
    for node, neighbors in graph.items():
        row.extend([node] * len(neighbors))
        col.extend(neighbors)
    edge_index = torch.tensor([row, col], dtype=torch.long)


    num_nodes = x.shape[0]
    train_mask = torch.zeros(num_nodes, dtype=torch.bool)
    val_mask = torch.zeros(num_nodes, dtype=torch.bool)
    test_mask = torch.zeros(num_nodes, dtype=torch.bool)
    train_mask[:140] = True
    val_mask[140:640] = True
    test_mask[1708:] = True

    x = torch.tensor(x, dtype=torch.float)
    y = torch.tensor(y.argmax(axis=1), dtype=torch.long)

    return Data(x=x, edge_index=edge_index, y=y, 
                train_mask=train_mask, val_mask=val_mask, test_mask=test_mask)


def evaluate(emb, data, mask):
    train_mask_cpu = data.train_mask.cpu().numpy()
    y_cpu = data.y.cpu().numpy()
    mask_cpu = mask.cpu().numpy()
    clf = LogisticRegression(multi_class='multinomial', solver='lbfgs', max_iter=1000)
    clf.fit(emb[train_mask_cpu], y_cpu[train_mask_cpu])
    pred = clf.predict(emb[mask_cpu])
    return accuracy_score(y_cpu[mask_cpu], pred)



data = load_local_cora().to(DEVICE)
print(f"Dataset: {data}")


print("Building hypergraph with batch processing...")
hyperedge_index = build_knn_hypergraph(data.x, k=KNN_K).to(DEVICE)
print(f"Hypergraph built with {hyperedge_index.size(1)} incidences")


model = H_GCL(data.num_features, HIDDEN_CHANNELS, EMB_DIM).to(DEVICE)
optimizer = optim.AdamW(model.parameters(), lr=LR, weight_decay=WEIGHT_DECAY)


scheduler = lr_scheduler.ReduceLROnPlateau(
    optimizer, mode='max', factor=0.5, patience=10
)

# Training state
best_val_acc = 0
best_emb = None
patience_counter = 0
test_acc = 0.0  # Initialize test accuracy
best_loss = float('inf')
last_lr = LR

for epoch in range(1, NUM_EPOCHS + 1):
    model.train()
    optimizer.zero_grad()
    
    loss, emb = model(data.x, data.edge_index, hyperedge_index)
    loss.backward()
    optimizer.step()
    
    
    model.eval()
    with torch.no_grad():
        _, val_emb = model(data.x, data.edge_index, hyperedge_index)
    
    val_emb = val_emb.cpu().numpy()
    val_acc = evaluate(val_emb, data, data.val_mask)
    
    
    scheduler.step(val_acc)
    
    
    current_lr = optimizer.param_groups[0]['lr']
    if current_lr != last_lr:
        print(f"Learning rate reduced to {current_lr:.6f}")
        last_lr = current_lr
    
    # Track best model
    if val_acc > best_val_acc:
        best_val_acc = val_acc
        best_emb = val_emb.copy()
        patience_counter = 0
        test_acc = evaluate(best_emb, data, data.test_mask)
        best_loss = loss.item()
    elif val_acc == best_val_acc and loss.item() < best_loss:
        
        best_emb = val_emb.copy()
        test_acc = evaluate(best_emb, data, data.test_mask)
        best_loss = loss.item()
        patience_counter = 0
    else:
        patience_counter += 1
    
    
    print(f'Epoch {epoch:03d}, Loss: {loss.item():.4f}, '
          f'Val Acc: {val_acc:.4f}, Best Test Acc: {test_acc:.4f}, '
          f'LR: {current_lr:.6f}')
    if patience_counter >= PATIENCE:
        print(f"Early stopping at epoch {epoch}")
        break


if best_emb is None:
    best_emb = val_emb.copy()
    test_acc = evaluate(best_emb, data, data.test_mask)
    
print(f"\n=== Final Results ===")
print(f"Best Validation Accuracy: {best_val_acc:.4f}")
print(f"Test Accuracy: {test_acc:.4f}")

np.save("best_embeddings.npy", best_emb)
print("Best embeddings saved to best_embeddings.npy")