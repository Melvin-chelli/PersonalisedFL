from collections import OrderedDict
from typing import List, Tuple

import numpy as np
import flwr as fl
import random
import time
import torch
import torchvision.transforms as transforms

from utils.transforms import Rotate, LabelFlip, Invert, Equalize
from utils.function import train_standard_classifier, train_regression, test_standard_classifier, test_regression
from utils.clustering_fn import compute_low_dims_per_class
from utils.datasets import load_partition
from utils.partition_data import Partition

torch.manual_seed(0)
DEVICE='cuda' if torch.cuda.is_available() else 'cpu'
batch_size = 64

class StandardClient(fl.client.NumPyClient):
    def __init__(self, model, trainloader=None, valloader=None, sim=True, args={}):
        self.model = model
        self.sim = sim
        self.args = args
        if not sim:
            self.trainloader = trainloader
            self.valloader = valloader
            self.transform = args["transform"]

    def get_parameters(self, config=None):
        return [val.cpu().numpy() for _, val in self.model.state_dict().items()]

    def set_parameters(self, parameters):
        params_dict = zip(self.model.state_dict().keys(), parameters)
        state_dict = OrderedDict({k: torch.from_numpy(v) for k, v in params_dict})
        self.model.load_state_dict(state_dict, strict=True)

    def fit(self, parameters, config):
        if self.sim:
            self.transform = config['transform']
            self.load_partition(partition=config['partition'], transform_instruction=config['transform'])

        # local training
        self.set_parameters(parameters)
        if self.args["model"] == 'cnn':
            train_standard_classifier(self.model, self.trainloader, config=config, device=DEVICE, args=self.args)
        elif self.args["model"] == 'regression':
            train_regression(self.model, self.trainloader, config=config, device=DEVICE, args=self.args)

        params = self.get_parameters()

        return params, len(self.trainloader), {"transform": self.transform}

    def evaluate(self, parameters, config):
        self.set_parameters(parameters)
        if self.args["model"] == 'cnn':
            loss, accuracy = test_standard_classifier(self.model, self.valloader, device=DEVICE)
        elif self.args["model"] == 'regression':
            loss, accuracy = test_regression(self.model, self.valloader, device=DEVICE)

        print(f"EVAL model performance - accuracy: {accuracy}, loss: {loss}")

        return float(loss), len(self.valloader), {"accuracy": float(accuracy)}

    def load_partition(self, partition, transform_instruction):
        print(f"Loading partition {partition} with transform {transform_instruction}")
        transform, target_transform = load_transform(transform_instruction)
        self.trainloader, self.valloader, _ = load_partition(partition, batch_size, transform=transform, target_transform=target_transform)


class EncodingClient(StandardClient):
    def __init__(self, model, trainloader=None, valloader=None, sim=True, embedding_model=None, args={}):
        super(EncodingClient, self).__init__(model, trainloader, valloader, sim, args)
        self.embedding_model = embedding_model

    def fit(self, parameters, config):
        if config["task"] == "compute_low_dim":
            # load partition to use
            if self.sim:
                self.transform = config['transform']
                self.load_partition(partition=config['partition'], transform_instruction=config['transform'])
            s = time.time()
            # compressing local data
            print("CLIENT NUM: ", config["client_number"])
            if self.args["compression"] == 'AE' and self.args["dataset"] == 'cifar10':
                self.trainloader.dataset.transform = transforms.Compose([transforms.ToTensor(), transforms.Grayscale()])
            sample_flat_dim = self.trainloader.dataset[0][0].flatten().size()[0]
            ld = compute_low_dims_per_class(self.embedding_model, 
                self.trainloader, 
                output_size=self.args['z_dim'], 
                device=DEVICE, 
                style_extraction=self.args['style_extraction'], 
                sample_size=sample_flat_dim,
                n_classes=self.args['n_classes']
            )
            print(len(ld))
            if self.args["compression"] == 'AE' and self.args["dataset"] == 'cifar10':
                self.trainloader.dataset.transform = transforms.Compose([transforms.ToTensor()])
            t = time.time() - s
            self.t_comp = t
            return [np.array(ld)], len(self.trainloader), {"transform": self.transform, "client_number": config["client_number"]}
        else:
            # local training
            print("CLIENT NUM: ", config["client_number"])
            s = time.time()
            self.set_parameters(parameters)
            if self.args["model"] == 'cnn':
                train_standard_classifier(self.model, self.trainloader, config=config, device=DEVICE, args=self.args)
            elif self.args["model"] == 'regression':
                train_regression(self.model, self.trainloader, config=config, device=DEVICE, args=self.args)

            params = self.get_parameters()
            t = time.time() - s
            self.t_train = t
            print(f"t_comp {self.t_comp}, t_train {self.t_train}, ratio {self.t_comp / self.t_train}")
            return params, len(self.trainloader), {"transform": self.transform, "client_number": config["client_number"]}


    def evaluate(self, parameters, config):
        self.set_parameters(parameters)
        if self.args["model"] == 'cnn':
            loss, accuracy = test_standard_classifier(self.model, self.valloader, device=DEVICE)
        elif self.args["model"] == 'regression':
            loss, accuracy = test_regression(self.model, self.valloader, device=DEVICE)

        print(f"Cluster {config['cluster_id']} EVAL model performance - accuracy: {accuracy}, loss: {loss} | transform {self.args['transform']}")

        return float(loss), len(self.valloader), {"accuracy": float(accuracy), "cluster_id": config['cluster_id']}



class IFCAClient(StandardClient):
    def __init__(self, model, trainloader=None, valloader=None, sim=True, args={}):
        super(IFCAClient, self).__init__(model, trainloader, valloader, sim, args)

    def fit(self, parameters, config):
        if self.sim:
            self.transform = config['transform']
            self.load_partition(partition=config['partition'], transform_instruction=config['transform'])

        # Estimating cluster identity
        if config["round"] == 1:
            cluster_id = random.randint(0, config["n_clusters"]-1)
        else:
            cluster_id = self.estimate_cluster_identity(parameters, n_clusters=config["n_clusters"], n_base_layers=config["n_base_layers"], n_pers_layers=config["n_pers_layers"])

        # Assigning corresponding model for local training
        print(f"Assigning client to cluster {cluster_id}")
        n_base_layers = config["n_base_layers"]
        n_pers_layers = config["n_pers_layers"]
        
        self.set_parameters(parameters[:n_base_layers] + parameters[n_base_layers:][n_pers_layers*cluster_id : n_pers_layers*(cluster_id+1)])

        if self.args["model"] == 'cnn':
            train_standard_classifier(self.model, self.trainloader, config=config, device=DEVICE, args=self.args)
        elif self.args["model"] == 'regression':
            train_regression(self.model, self.trainloader, config=config, device=DEVICE, args=self.args)

        params = self.get_parameters()

        return params, len(self.trainloader), {"transform": self.transform, "cluster_id": cluster_id, "client_number": config["client_number"]}

    def evaluate(self, parameters, config):
        self.set_parameters(parameters)
        if self.args["model"] == 'cnn':
            loss, accuracy = test_standard_classifier(self.model, self.valloader, device=DEVICE)
        elif self.args["model"] == 'regression':
            loss, accuracy = test_regression(self.model, self.valloader, device=DEVICE)

        print(f"EVAL model performance - accuracy: {accuracy}, loss: {loss}")

        return float(loss), len(self.valloader), {"accuracy": float(accuracy), "cluster_id": config['cluster_id']}

    def estimate_cluster_identity(self, parameters, n_base_layers, n_pers_layers, n_clusters=2):
        id = 0
        for i in range(n_clusters):
            self.set_parameters(parameters[:n_base_layers] + parameters[n_base_layers:][n_pers_layers*i : n_pers_layers*(i+1)])
            if self.args["model"] == 'cnn':
                loss, accuracy = test_standard_classifier(self.model, self.valloader, device=DEVICE)
            elif self.args["model"] == 'regression':
                loss, accuracy = test_regression(self.model, self.valloader, device=DEVICE)
            if i==0:
                best_loss = loss
            elif loss < best_loss:
                best_loss = loss
                id = i

        return id



def load_transform(transform_instruction):
    target_transform=None
    transform=None
    if transform_instruction == "solarize":
        transform = transforms.Compose([transforms.RandomSolarize(threshold=200.0), transforms.ToTensor()])
    elif transform_instruction == "invert":
        transform = transforms.Compose([Invert(), transforms.ToTensor()])
    elif transform_instruction == "equalize":
        transform = transforms.Compose([Equalize(), transforms.ToTensor()])
    elif transform_instruction == "elastic":
        transform = transforms.Compose([transforms.ElasticTransform(alpha=100.0), transforms.ToTensor()])
    elif transform_instruction == "rotate90":
        transform = transforms.Compose([Rotate(90), transforms.ToTensor()])
    elif transform_instruction == "rotate180":
        transform = transforms.Compose([Rotate(180), transforms.ToTensor()])
    elif transform_instruction == "rotate270":
        transform = transforms.Compose([Rotate(270), transforms.ToTensor()])
    else:
        transform = transforms.Compose([transforms.ToTensor()])
        
    if transform_instruction == "label_flip_1":
        target_transform = LabelFlip(1)
    elif transform_instruction == "label_flip_2":
        target_transform = LabelFlip(2)
    elif transform_instruction == "label_flip_3":
        target_transform = LabelFlip(3)
    elif transform_instruction == "label_flip_4":
        target_transform = LabelFlip(4)

    return transform, target_transform