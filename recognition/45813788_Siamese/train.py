import os
import torch
from torch.nn import BCEWithLogitsLoss
from modules import SiameseNN, Classifier
from utils import visualise_embedding, plot_loss, plot_accuracy, plot_auc
from pytorch_metric_learning.losses import ContrastiveLoss
from pytorch_metric_learning.reducers import AvgNonZeroReducer
from pytorch_metric_learning.distances import LpDistance
from tqdm import tqdm
from torch.optim.lr_scheduler import ReduceLROnPlateau
from sklearn.metrics import roc_auc_score, classification_report
import numpy as np



def siamese_train(current_dir, train_loader, val_loader, images, epochs=50, lr=1e-4, plots=False):
    
    save_dir = os.path.join(current_dir,'models')

    print("Training Siamese Network")
    # Initialize model and move to device
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model = SiameseNN().to(device)

    # Contrastive Loss from PyTorch Metric Learning
    criterion = ContrastiveLoss(
        distance=LpDistance(normalize_embeddings=True, p=2, power=1),
    )
    
    # optimizer
    optimizer = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=1e-5)

    #scheduler
    scheduler = ReduceLROnPlateau(optimizer, mode='min', factor=0.1, patience=3, threshold=0.01)

    # Training parameters
    best_loss = float('inf')
    train_losses = []
    val_losses = []

    for epoch in range(epochs):
        # Training phase
        model.train()
        epoch_loss = 0.0

        for images, labels  in tqdm(train_loader, desc=f"Epoch {epoch+1}/{epochs} Training"):
            images, labels = images.to(device), labels.to(device)
            
            optimizer.zero_grad()
            # Forward pass
            embeddings = model(images)            
            loss = criterion(embeddings, labels)
            # Backward pass and optimization
            loss.backward()
            optimizer.step()

            epoch_loss += loss.item()

        avg_train_loss = epoch_loss / len(train_loader)
        train_losses.append(avg_train_loss)

        # Validation
        model.eval()
        val_loss = 0.0
        all_embeddings = []
        all_labels = []
        with torch.no_grad():
            for images, labels in tqdm(val_loader, desc=f"Epoch {epoch+1}/{epochs} Validating"):
                images, labels = images.to(device), labels.to(device)

                # Forward pass
                embeddings = model(images)

                loss = criterion(embeddings, labels)

                val_loss += loss.item()

                # Collect embeddings and labels for visualization
                all_embeddings.append(embeddings.cpu())
                all_labels.extend(labels.cpu().numpy())

        avg_val_loss = val_loss / len(val_loader)
        val_losses.append(avg_val_loss)

        print(f"Epoch [{epoch+1}/{epochs}] - Train Loss: {avg_train_loss:.4f} - Val Loss: {avg_val_loss:.4f}")
        # Step the scheduler
        scheduler.step(avg_val_loss)
        # Monitor learning rate
        for idx, param_group in enumerate(optimizer.param_groups):
            print(f"Learning rate for param group {idx}: {param_group['lr']}")

        # save current pest model
        if avg_val_loss < best_loss:
            best_loss = avg_val_loss
            file_name = 'siamese_resnet18_best.pth'
            save_path = os.path.join(save_dir, file_name)
            torch.save(model.state_dict(), save_path)
            print("Validation loss decreased. Saving model.")
        else:
            print("No improvement in validation loss.")

        # Visualize embeddings using t-SNE and PCA
        if plots == True:
            all_embeddings_tensor = torch.cat(all_embeddings)
            visualise_embedding(all_embeddings_tensor, all_labels, epoch+1, current_dir)


    # Save the final model
    file_name = 'siamese_resnet18.pth'
    save_path = os.path.join(save_dir, file_name)
    torch.save(model.state_dict(), save_path)
    print("Training complete. Models saved.")

    if plots == True:
        plot_loss(train_losses,val_losses)


def classifier_train(current_dir, train_loader, val_loader, images, siamese, epochs=50, plots=False):

    print("Training classifier")
    save_dir = os.path.join(current_dir,'models')
  

    # Initialize model and move to device
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    
    siamese.to(device)
    siamese.eval()
    classifier = Classifier().to(device)
    classifier.train()

    # optimizer
    optimizer = torch.optim.Adam(classifier.parameters(), lr=1e-4, weight_decay=1e-5)

    #scheduler
    #scheduler = ReduceLROnPlateau(optimizer, mode='min', factor=0.1, patience=3, threshold=0.01)

    
    # Count of each class
    labels = [label for _, label in train_loader.dataset]
    benign_count = labels.count(0)
    malignant_count = labels.count(1)

    # Compute pos_weight for the minority class
    pos_weight = torch.tensor([benign_count / malignant_count], dtype=torch.float).to(device)


    #Criterion
    criterion = BCEWithLogitsLoss(pos_weight=pos_weight)
    #criterion = BCELoss()

    #Training parameters
    best_auroc = 0.0
    train_losses = []
    val_losses = []
    train_accuracies = []
    val_accuracies = []
    train_aurocs = []
    val_aurocs = []

    for epoch in range(epochs):
        # Training phase
        classifier.train()
        epoch_loss = 0.0
        correct_train = 0
        total_train = 0
        train_labels = []
        train_probs = []

        for images, labels  in tqdm(train_loader, desc=f"Epoch {epoch+1}/{epochs} Training"):
            images, labels = images.to(device), labels.to(device).float()
           
            #pass through siamese to get features then pass through classifer
            with torch.no_grad():
                embeddings = siamese(images)

            optimizer.zero_grad()
            output = classifier(embeddings).squeeze()
            loss = criterion(output, labels)
            loss.backward()
            optimizer.step()

            epoch_loss += loss.item()
            
            probs = torch.sigmoid(output)

            preds = (probs >= 0.5).float()
            correct_train += (preds == labels).sum().item()
            total_train += labels.size(0)

            train_labels.extend(labels.cpu().numpy())
            train_probs.extend(probs.cpu().detach().numpy()) 

        avg_train_loss = epoch_loss / len(train_loader)
        train_losses.append(avg_train_loss)
        train_accuracy = correct_train / total_train
        train_accuracies.append(train_accuracy)

        # Compute AUROC for training
        try:
            train_auroc = roc_auc_score(train_labels, train_probs)
            binary_train_probs = [1 if p >= 0.5 else 0 for p in train_probs] 
            print("Training Classification Report:")
            print(classification_report(train_labels, binary_train_probs, zero_division=0))
            

        except ValueError:
            train_auroc = np.nan  # Handle cases where AUROC is undefined

        train_aurocs.append(train_auroc)


        # Validation
        classifier.eval()
        val_loss = 0.0
        correct_val = 0
        total_val = 0

        val_labels = []
        val_probs = []

        with torch.no_grad():
            for images, labels in tqdm(val_loader, desc=f"Epoch {epoch+1}/{epochs} Validating"):
                images, labels = images.to(device), labels.to(device).float()

                #pass through siamese to get features then pass through classifer
                with torch.no_grad():
                    embeddings = siamese(images)

                
                output = classifier(embeddings).squeeze()
                loss = criterion(output, labels)
                
                val_loss += loss.item()

                probs = torch.sigmoid(output)
                preds = (probs >= 0.5).float()
                correct_val += (preds == labels).sum().item()
                total_val += labels.size(0)

                # Store for AUROC
                val_labels.extend(labels.cpu().numpy())
                val_probs.extend(probs.cpu().numpy())


        avg_val_loss = val_loss / len(val_loader)
        val_losses.append(avg_val_loss)
        val_accuracy = correct_val / total_val
        val_accuracies.append(val_accuracy)

        # Compute AUROC for validation
        try:
            val_auroc = roc_auc_score(val_labels, val_probs)
            binary_val_probs = [1 if p >= 0.5 else 0 for p in val_probs] 
            print("Validation Classification Report:")
            print(classification_report(val_labels, binary_val_probs, zero_division=0))

        except ValueError:
            val_auroc = np.nan  # Handle cases where AUROC is undefined

        val_aurocs.append(val_auroc)

        print(f"Epoch [{epoch+1}/{epochs}] - Train Loss: {avg_train_loss:.4f} - Val Loss: {avg_val_loss:.4f}")
        print(f"Train Accuracy: {train_accuracy:.4f} - Val Accuracy: {val_accuracy:.4f}")
        print(f"Train AUROC: {train_auroc:.4f} - Val AUROC: {val_auroc:.4f}")

        # Step the scheduler
        #scheduler.step(avg_val_loss)

        # Monitor learning rate
        for idx, param_group in enumerate(optimizer.param_groups):
            print(f"Learning rate for param group {idx}: {param_group['lr']}")

        # save current pest model
        if val_auroc > best_auroc:
            best_auroc = val_auroc
            file_name = 'classifier_best.pth'
            save_path = os.path.join(save_dir, file_name)
            torch.save(classifier.state_dict(), save_path)
            print("Validation Auroc improved. Saving model.")
        else:
            print("No improvement in validation Auroc.")


    # Save the final model
    file_name = 'classifier.pth'
    save_path = os.path.join(save_dir, file_name)
    torch.save(classifier.state_dict(), save_path)
    print("Classifier Training complete. Models saved.")

    if plots == True:
        plot_loss(train_losses,val_losses)
        plot_accuracy(train_accuracies,val_accuracies)
        plot_auc(train_aurocs, val_aurocs)


