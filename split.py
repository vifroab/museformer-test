import os
import splitfolders
import numpy as np
from tools import generate_token_data_by_file_list


token_dir= "data/token-piano"
split_dir="data/split-piano"
meta_dir = "data/meta/"


def rename_all_files(directory_path):
	# Loop through all files in the directory
	for filename in os.listdir(directory_path):
		# Generate the new filename by replacing - and blank spaces with _
		new_filename = filename.replace("-", "_").replace(" ", "_")

		# Get the full paths for the old and new filenames
		old_filepath = os.path.join(directory_path, filename)
		new_filepath = os.path.join(directory_path, new_filename)

		# Rename the file
		os.rename(old_filepath, new_filepath)


def write_filenames(filenames: [], meta_filename):
	# Write the filenames to the output file
	with open(meta_dir+meta_filename, 'w') as f:
		for filename in filenames:
			f.write(f"{filename}\n")

def save_split_in_3_files():
	filenames = os.listdir(token_dir)
	train_size =0.8
	validate_size = 0.1
	train, validate, test = np.split(filenames, [int(train_size * len(filenames)), int((validate_size + train_size) * len(filenames))])
	write_filenames(train, "train.txt")
	write_filenames(validate, "valid.txt")
	write_filenames(test, "test.txt")

def split_tokens():
	for split in ["train" ,"valid", "test"]:
		generate_token_data_by_file_list.main(f'data/meta/{split}.txt' ,token_dir, split_dir)


def main():
	rename_all_files(token_dir)
	save_split_in_3_files()
	split_tokens()


if __name__ == '__main__':
	main()

