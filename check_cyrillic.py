import os
import re
import glob

def count_cyrillic_lines(file_path):
    """Count lines containing Cyrillic characters in a file."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        cyrillic_count = 0
        total_lines = len(lines)
        
        for line in lines:
            # Check if line contains Cyrillic characters
            if re.search(r'[а-яА-ЯёЁ]', line):
                cyrillic_count += 1
                
        return cyrillic_count, total_lines
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return 0, 0

def main():
    # Directories to check
    directories = [
        'actions',
        'memory',
        'dashboard',
        'plugins'
    ]
    
    # Files to check
    files_to_check = []
    
    # Add all .py files from directories
    for directory in directories:
        pattern = os.path.join(directory, '*.py')
        files_to_check.extend(glob.glob(pattern))
    
    # Add the specific template file
    template_file = 'plugins/_template.py'
    if os.path.exists(template_file):
        files_to_check.append(template_file)
    
    # Results list
    results = []
    
    for file_path in files_to_check:
        cyrillic_count, total_lines = count_cyrillic_lines(file_path)
        percentage = (cyrillic_count / total_lines * 100) if total_lines > 0 else 0
        results.append({
            'file': file_path,
            'cyrillic_lines': cyrillic_count,
            'total_lines': total_lines,
            'percentage': percentage
        })
    
    # Sort by percentage (ascending)
    results.sort(key=lambda x: x['percentage'])
    
    # Print top 20 files with fewest Cyrillic lines
    print("Топ 20 файлов с наименьшим количеством кириллических строк:")
    print("=" * 60)
    for i, result in enumerate(results[:20]):
        print(f"{i+1:2d}. {result['file']}")
        print(f"    Кириллических строк: {result['cyrillic_lines']}/{result['total_lines']} ({result['percentage']:.1f}%)")
        print()

if __name__ == '__main__':
    main()