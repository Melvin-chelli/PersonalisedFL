import torch
import numpy as np
from sklearn.cluster import MiniBatchKMeans, KMeans
from sklearn.metrics import silhouette_score
from sklearn.metrics.cluster import adjusted_rand_score
import logging
from utils.style_extraction import StyleExtractor

def select_samples(dataloader, label, device):
    chosen_indices = []
    samples = []
    for _, (x, y) in enumerate(dataloader):
        for i in range(len(x)):
            if y[i].detach().cpu().numpy() == label:
                samples.append(x[i].to(device))
            
    return samples


def split_by_class(dataloader, n_classes, device):
    samples = [[] for _ in range(n_classes)]
    for _, (x, y) in enumerate(dataloader):
        for i in range(len(x)):
            samples[y[i].detach().cpu().numpy()].append(x[i].to(device))

    return samples


def compute_low_dims(net, batch, output_size, device):
    if len(batch) == 0:
        #print("Empty class batch - Generating Random Low dim")
        return torch.rand(1, output_size).to(device)
    else:
        return net(torch.stack(batch)).to(device)


def extract_style(batch, extractor, sample_size):
    if len(batch) == 0:
        #print("Empty class batch - Generating Random Low dim")
        if sample_size == 784:
            return torch.rand(1, 25).numpy()
        if sample_size == 3072:
            return torch.rand(1, 147).numpy()
    else:
        styles = []
        for x in batch:
            style = np.array(extractor._extract_style(x.cpu())).flatten()
            styles.append(style)
        styles = np.stack(styles, axis=0)
        return styles


def compute_avg(low_dim):
    return low_dim.mean(0).cpu().detach().numpy()


def compute_low_dims_per_class(net, dataloader, output_size, device, style_extraction=False, sample_size=784, n_classes=10):
    ld = np.array([])
    class_batches = split_by_class(dataloader, n_classes, device=device)
    for class_id in range(n_classes):
        if style_extraction:
            extractor = StyleExtractor()
            ld = np.append(ld, compute_avg(torch.Tensor(extract_style(class_batches[class_id], extractor, sample_size))))
        else:
            ld = np.append(ld, compute_avg(compute_low_dims(net, class_batches[class_id], output_size, device)))

    return ld


def make_clusters(low_dims, n_clusters, n_clients, kmeans=None, find_optimal=False):
    if find_optimal:
        optim_clusters = find_optimal_clustering(low_dims, n_clusters)
        print("Optimal number of clusters: ", optim_clusters)
    if kmeans is None:
        kmeans = MiniBatchKMeans(n_clusters=n_clusters, n_init=10, batch_size=n_clients).partial_fit(low_dims)
    else:
        kmeans = kmeans.partial_fit(low_dims)
    centers = kmeans.cluster_centers_
    labels = kmeans.labels_

    return labels, centers, kmeans


def find_optimal_clustering(X, max_clusters):
    optimal_number = 0
    optimal_silhouette = -1
    for i in range(2, max_clusters+1):
        kmeans = KMeans(n_clusters=i)
        cluster_labels = kmeans.fit_predict(X)
        score = silhouette_score(X, cluster_labels)
        print(f"n_clusters: {i} - score: {score}")
        if score > optimal_silhouette:
            optimal_number = i
            optimal_silhouette = score
    return optimal_number


def print_clusters(labels, cluster_truth, n_clusters):
    logging.basicConfig(filename="log_traces.log", level=logging.INFO)
    for i in range(n_clusters):
        print(f'CLUSTER {i}:')
        logging.info(f'CLUSTER {i}:')
        for j in range(len(labels)):
            if labels[j] == i:
                print(f"Client {j} - {cluster_truth[j]}")
                logging.info(f"Client {j} - {cluster_truth[j]}")
        print("####")
        logging.info("####")



def compute_clustering_acc(labels, cluster_truth, n_clusters):
    '''The Rand Index computes a similarity measure between two clusterings by considering 
    all pairs of samples and counting pairs that are assigned in the same or different 
    clusters in the predicted and true clusterings.'''
    clusters = [[] for _ in range(n_clusters)]
    n_clients = len(labels)

    # Convert clustering ground truth to list of digits
    ground_truth = []
    true_labels = []
    last_index = 0
    for label in cluster_truth:
        if label in true_labels:
            ground_truth.append(true_labels.index(label))
        else:
            true_labels.append(label)
            ground_truth.append(last_index)
            last_index += 1

    # Compute Rand Index
    score = adjusted_rand_score(labels, cluster_truth)
    
    return score