## Introduction
This project leverages the job posting data from LinkedIn to figure out the required technical skills of different majors and forms datasets. Then we conducted the Gap Analysis between skills dataset and curriculum of Universities to figure out the missing courses or topics, which could be a valid evidence to initiate the courses revolution of specific department.

## Files
**Computer_science_glossary_terms.xlsx**: This file contains computer science glossary terms from Wikipedia, which could be regarded as techincal skills corpus of Computer Science.

**LinkedIn_Scrapper_for_Mac.ipynb**: This file provides a Python-based web scraper designed to extract data from LinkedIn Job Postings using Selenium on macOS. It automates browser interactions to collect publicly available information such as job titles, companies, locations, and etc. 

**NER.ipynb**: This file uses spaCy to build a custom matcher that identifies domain-specific entities from Computer Science terms by analyzing sections such as requirements, job descriptions, qualifications, preferred qualifications, and responsibilities. Before run this file, please use command **python -m spacy download en_core_web_sm** first please. 


**Statistic_Visualization.ipynb**: This file counts the term frequency in CS terms extracted from job requirements, qualifications and so on. 

**LLama-pipeline**: This file used LLama-3-8B-Instruct to summarize the wanted computer science skills from industry. It leverages the ability of small language model to accomplish long context understanding. 